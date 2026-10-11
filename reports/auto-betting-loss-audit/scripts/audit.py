#!/usr/bin/env python3
"""Read-only capture and reproducible attribution; never submits a bet."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

REPO = Path(__file__).resolve().parents[3]
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from core.settlement import pnl_for, settle_pick, split_line as current_split_line

GRADED = {'won', 'lost', 'push', 'half_won', 'half_lost'}
PROFIT_TOLERANCE = 0.025  # Venue rounds monetary returns to cents.
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 40
QUANTILE_LOW = 0.025
QUANTILE_HIGH = 0.975
SIGN_PATTERN = re.compile(r'([+-]?\d+(?:\.\d+)?(?:/[+-]?\d+(?:\.\d+)?)?)\s*$')
SCORE_PATTERN = re.compile(r'(\d+)\s*[:\-]\s*(\d+)')
EVIDENCE_FILES = ('service/live_expert.py','service/betting.py','core/live_model.py',
                  'core/ensemble.py','core/settlement.py','core/economics.py',
                  'store/training_data.py','service/ledger.py')


def split_line(value):
    """Freeze the audited v1 sign semantics even after production is repaired."""
    parts = str(value).split('/')
    if len(parts)==2:
        try:
            value = '/'.join('%+g' % float(part) for part in parts)
        except ValueError:
            return []
    return current_split_line(value)


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def write_csv(path, rows, fields=None):
    rows = list(rows)
    fields = fields or (list(rows[0]) if rows else [])
    if not fields:
        raise ValueError('CSV schema is required for empty data')
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def score(value):
    match = SCORE_PATTERN.search(str(value or ''))
    return tuple(map(int, match.groups())) if match else None


def inherited_line(value):
    """Audit interpretation of provider -0/0.5 = -0/-0.5, not +0.25.

    This deliberately does not mutate production split_line(). Explicit signs
    on the second leg are preserved. This interpretation is checked against
    actual venue profit rather than assumed for all future markets.
    """
    raw = str(value).strip()
    parts = raw.split('/')
    if len(parts) == 2 and raw.startswith('-') and not parts[1].startswith(('-', '+')):
        parts[1] = '-' + parts[1]
    result = split_line('/'.join(parts))
    if not result:
        raise ValueError('invalid handicap')
    return '/'.join(str(v) for v in result)


def grade(market, outcome, line, result, benchmark=None, inherit=False):
    if result is None:
        return 'void'
    if inherit and market.startswith('AH'):
        line = inherited_line(line)
    if benchmark is not None and market.startswith('AH'):
        result = tuple(a-b for a, b in zip(result, benchmark))
        if min(result) < 0:
            return 'void'
    if not inherit and market.startswith('AH'):
        old_parts = split_line(line)
        if not old_parts:
            return 'void'
        line = '/'.join('%+g' % part for part in old_parts)
    return settle_pick(market, outcome, line,
                       ft_score=result if not market.endswith('_1H') else None,
                       ht_score=result if market.endswith('_1H') else None)[0]


def venue_line(row):
    match = SIGN_PATTERN.search(str(row.get('market_value') or ''))
    if not match:
        raise ValueError('venue display line absent')
    line = inherited_line(match.group(1))
    if str(row.get('play_options')) == '2':
        line = '/'.join(str(-float(p)) for p in line.split('/'))
    return line


def capture(source_root, venue_path):
    data = ROOT / 'data'
    data.mkdir(parents=True, exist_ok=True)
    captured = datetime.now(timezone.utc).isoformat()
    source_path = source_root / 'output/ledger/ledger.sqlite3'
    db = sqlite3.connect(source_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('BEGIN')
    summaries = []
    for r in db.execute("""SELECT trigger,algorithm,market,count(*) records,
        count(DISTINCT match_id) matches,
        sum(status IN ('won','lost','half_won','half_lost','push')) graded,
        sum(status='pending') pending, sum(status='void') void,
        sum(CASE WHEN status IN ('won','lost','half_won','half_lost','push') THEN pnl ELSE 0 END) profit_units,
        sum(abs(p_fused-1)<0.000001) p_equal_one,
        avg(p_fused) mean_p_model,avg(p_market) mean_p_market
        FROM decisions GROUP BY trigger,algorithm,market"""):
        summaries.append(dict(r))
    columns = ('match_id', 'at', 'market', 'line', 'outcome', 'odds', 'status',
               'pnl', 'p_fused', 'p_market', 'edge', 'confidence', 'kelly',
               'entry_score', 'ft_score', 'ht_score', 'entry_clock_s', 'config_version', 'weights')
    rows = []
    for (payload,) in db.execute("SELECT payload FROM decisions WHERE "
        "trigger='live_recommendation' AND algorithm='economic_ensemble' ORDER BY at,identity"):
        raw = json.loads(payload)
        row = {key: raw.get(key) for key in columns}
        for key in ('entry_score', 'ft_score', 'ht_score', 'weights'):
            row[key] = json.dumps(row[key], separators=(',', ':'))
        rows.append(row)
    all_records = dict(db.execute('SELECT count(*) records,min(at) first_at,max(at) last_at FROM decisions').fetchone())
    db.close()
    write_csv(data / 'ledger_groups.csv', summaries)
    write_csv(data / 'ensemble_recommendations.csv', rows, list(columns))
    db = sqlite3.connect((source_root / 'output/betting-orders.sqlite3').resolve().as_uri()+'?mode=ro', uri=True, timeout=5)
    orders = [json.loads(r[0]) for r in db.execute('SELECT payload FROM orders ORDER BY at')]
    db.close()
    by_order = {r['order_no']: r for r in orders if r.get('order_no')}
    fields = ('match_id', 'market', 'line', 'outcome', 'status', 'submitted', 'stake',
              'checked_at_ms', 'decision_at_ms', 'quote_to_submit_ms', 'latency_target_met')
    order_rows = [{'order_alias': hashlib.sha256(str(r.get('order_no') or r['identity']).encode()).hexdigest()[:12],
                   **{key: r.get(key) for key in fields}} for r in orders]
    write_csv(data / 'execution_orders.csv', order_rows)
    venue = json.loads(venue_path.read_text())
    receipts = []
    for raw in venue['rows']:
        local = by_order.get(raw['order_no'])
        row = {k:v for k,v in raw.items() if k not in ('order_no', 'option', 'market_value')}
        row['order_alias'] = hashlib.sha256(raw['order_no'].encode()).hexdigest()[:12]
        row['matched_auto'] = int(local is not None)
        row['native_market'] = {4:'AH', 19:'AH_1H', 1:'HAD'}.get(int(raw['play_id']), 'UNKNOWN')
        row['outcome'] = {'1':'home', '2':'away', 'X':'draw'}.get(str(raw['play_options']), '')
        try:
            row['line'] = local['line'] if local else venue_line(raw)
            row['canonical_line'] = venue_line(raw)
        except ValueError:
            row['line'] = row['canonical_line'] = ''
        receipts.append(row)
    write_csv(data / 'venue_settled.csv', receipts)
    config = json.loads((source_root / 'output/runtime-settings.json').read_text())
    config['settings'] = {k:v for k,v in config['settings'].items()
                          if not any(s in k for s in ('key', 'base_url', 'fallback', 'llm_model'))}
    dump(data / 'runtime_settings.json', config)
    model = json.loads((source_root / 'output/models/dashboard.json').read_text())
    model = {'mode': model.get('mode'), 'note': model.get('note'), 'training': model.get('training'),
             'prospective': model.get('prospective'), 'model': {k:v for k,v in model.get('model',{}).items()
                        if k not in ('weights', 'cohort_ids', 'features')}}
    dump(data / 'model_snapshot.json', model)
    code_commit = subprocess.check_output(['git','rev-parse','--short','HEAD'],cwd=REPO,text=True).strip()
    dump(data / 'capture.json', {'captured_at': captured, 'code_commit': code_commit,
        'ledger': all_records, 'ledger_groups':len(summaries), 'ensemble_records':len(rows),
        'venue_fetched_at_ms':venue['fetched_at_ms'], 'venue_complete':venue['complete'],
        'venue_total_per_page':venue['total_per_page'], 'venue_unique_count':venue['unique_count'],
        'execution_records':len(orders), 'database_snapshot_note':'每个SQLite库分别用一致只读视图；跨库不是同一事务',
        'source_hashes':{name:hashlib.sha256((REPO/name).read_bytes()).hexdigest() for name in EVIDENCE_FILES}})
    print('capture', len(rows), 'recommendations;', len(receipts), 'receipts;', len(orders), 'execution intents')


def read_csv(name):
    with (ROOT / 'data' / name).open(encoding='utf-8', newline='') as source:
        return list(csv.DictReader(source))


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('non-finite number')
    return result


def describe_money(rows, profit_key='profit'):
    stake = sum(number(r['stake']) for r in rows)
    profit = sum(number(r[profit_key]) for r in rows)
    return {'orders':len(rows), 'matches':len({r['match_id'] for r in rows}),
            'stake':round(stake, 3), 'profit':round(profit, 3), 'roi':profit/stake if stake else None}


def clustered_interval(rows):
    import random
    if not rows:
        return [None, None]
    groups = defaultdict(lambda: [0.0, 0.0])
    for row in rows:
        groups[row['match_id']][0] += number(row['stake'])
        groups[row['match_id']][1] += number(row['profit'])
    clusters = list(groups.values())
    rng = random.Random(BOOTSTRAP_SEED)
    samples = []
    for _ in range(BOOTSTRAP_REPLICATES):
        selected = rng.choices(clusters, k=len(clusters))
        samples.append(sum(r[1] for r in selected)/sum(r[0] for r in selected))
    samples.sort()
    return [samples[int(len(samples)*q)] for q in (QUANTILE_LOW, QUANTILE_HIGH)]


def analyze():
    rows = read_csv('venue_settled.csv')
    comparison = []
    for row in rows:
        if row['native_market'] not in ('AH', 'AH_1H') or not row['canonical_line']:
            continue
        final, entry = score(row['settle_score']), score(row['score_benchmark'])
        if final is None or entry is None:
            continue
        row = dict(row)
        for scenario, line, baseline in (('production', row['line'], None),
                                        ('full_correct_sign', row['canonical_line'], None),
                                        ('remaining_current_sign', row['line'], entry),
                                        ('remaining_correct_sign', row['canonical_line'], entry)):
            result = grade(row['native_market'], row['outcome'], line, final, baseline)
            row[scenario+'_status'] = result
            row[scenario+'_profit'] = round(pnl_for(result, number(row['odds']))*number(row['stake']),3)
            row[scenario+'_agrees'] = int(abs(row[scenario+'_profit']-number(row['profit']))<=PROFIT_TOLERANCE)
        comparison.append(row)
    write_csv(ROOT / 'data' / 'receipt_comparison.csv', comparison)
    auto = [r for r in comparison if r['matched_auto']=='1']
    real_auto = [r for r in rows if r['matched_auto']=='1']
    market_rows=[]
    for market in sorted({r['native_market'] for r in rows}):
        selected = [r for r in rows if r['native_market']==market]
        market_rows.append({'scope':'all_account','market':market,**describe_money(selected)})
        selected = [r for r in real_auto if r['native_market']==market]
        if selected:
            market_rows.append({'scope':'matched_auto','market':market,**describe_money(selected)})
    write_csv(ROOT / 'data' / 'actual_by_market.csv', market_rows)
    grouped = []
    for scenario in ('production', 'full_correct_sign', 'remaining_current_sign', 'remaining_correct_sign'):
        grouped.append({'scenario':scenario, 'orders':len(auto),
                        'agrees':sum(int(r[scenario+'_agrees']) for r in auto),
                        'profit':round(sum(r[scenario+'_profit'] for r in auto),3)})
    grouped.append({'scenario':'venue_actual','orders':len(auto),'agrees':len(auto),
                    'profit':round(sum(number(r['profit']) for r in auto),3)})
    write_csv(ROOT / 'data' / 'settlement_scenarios.csv', grouped)
    recommendations = read_csv('ensemble_recommendations.csv')
    corrected = []
    for r in recommendations:
        if r['status'] not in GRADED:
            continue
        r = dict(r)
        market = r['market']
        final = json.loads(r['ht_score'] if market.endswith('_1H') else r['ft_score'])
        entry = json.loads(r['entry_score'])
        if market.startswith('AH') and final and entry:
            r['corrected_status'] = grade(market,r['outcome'],r['line'],final,entry,inherit=True)
            r['corrected_pnl'] = pnl_for(r['corrected_status'],number(r['odds']))
        else:
            r['corrected_status'],r['corrected_pnl'] = r['status'],number(r['pnl'])
        r['production_pnl'] = number(r['pnl'])
        corrected.append(r)
    write_csv(ROOT / 'data' / 'ledger_counterfactual.csv', corrected)
    daily=[]
    for day in sorted({datetime.fromtimestamp(number(r['bet_time'])/1000,timezone.utc).date().isoformat() for r in real_auto}):
        picked=[r for r in real_auto if datetime.fromtimestamp(number(r['bet_time'])/1000,timezone.utc).date().isoformat()==day]
        daily.append({'utc_bet_date':day,**describe_money(picked)})
    write_csv(ROOT / 'data' / 'actual_by_date.csv', daily)
    orders=read_csv('execution_orders.csv')
    selections=defaultdict(list)
    for order in orders:
        if order['submitted']=='True':
            selections[(order['match_id'],order['market'],order['line'],order['outcome'])].append(order)
    duplicates=[values for values in selections.values() if len(values)>1]
    timed=[r for r in orders if r['quote_to_submit_ms']]
    frozen={'account_all':describe_money(rows),'matched_auto':describe_money(real_auto),
        'unmatched_account':describe_money([r for r in rows if r['matched_auto']=='0']),
        'auto_roi_cluster_bootstrap_95':clustered_interval(real_auto),
        'handicap_comparison':{'orders':len(auto),'actual':describe_money(auto),'scenarios':grouped},
        'ledger_first_recommendations':{'graded':len(corrected),
            'production_profit_units':round(sum(r['production_pnl'] for r in corrected),3),
            'remaining_signed_profit_units':round(sum(r['corrected_pnl'] for r in corrected),3),
            'changed_grades':sum(r['corrected_status']!=r['status'] for r in corrected),
            'matches':len({r['match_id'] for r in corrected})},
        'duplicates':{'groups':len(duplicates),'additional_submitted_intents':sum(len(r)-1 for r in duplicates),
                      'max_per_selection':max((len(r) for r in selections.values()),default=0)},
        'execution_status':dict(Counter(r['status'] for r in orders)),
        'latency':{'orders_with_metric':len(timed),'within_2000ms':sum(number(r['quote_to_submit_ms'])<=2000 for r in timed),
                   'max_ms':max((number(r['quote_to_submit_ms']) for r in timed),default=None)},
        'bootstrap':{'replicates':BOOTSTRAP_REPLICATES,'seed':BOOTSTRAP_SEED,'unit':'match'},
        'profit_tolerance_cny':PROFIT_TOLERANCE}
    dump(ROOT / 'data' / 'summary.json', frozen)
    print(json.dumps(frozen,ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-source',type=Path)
    parser.add_argument('--venue-json',type=Path)
    args=parser.parse_args()
    if args.capture_source:
        if args.venue_json is None:
            parser.error('--venue-json required for capture')
        capture(args.capture_source,args.venue_json)
    analyze()


if __name__ == '__main__':
    main()
