#!/usr/bin/env python3
"""Recompute current-ledger ROI, without overwriting fixed historical artifacts.

Needs PG_* environment variables and psycopg in an appropriate container.
Queries have no historical as-of or algorithm-version filter: a later run is
NOT a reproduction of the fixed 2026-10-13 snapshot. The separately supplied
order CSV has its own observation scope, which need not match current PG.
Outputs go to project output/independent-strategy-audit/current by default.
Bootstrap resamples matches, not correlated rows within the same match.
"""
import argparse
import csv
import json
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

from archive_privacy import REAL_ORDER_FIELDS, redact_real_order

REPORT = Path(__file__).resolve().parents[1]
PROJECT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = PROJECT / 'output/independent-strategy-audit/current'
GRADED = ('won', 'lost', 'half_won', 'half_lost', 'push')


def boot(groups, reps=2000, seed=20261013):
    keys = list(groups)
    rng = random.Random(seed)
    stake = sum(groups[k][0] for k in keys)
    pnl = sum(groups[k][1] for k in keys)
    draws = []
    for _ in range(reps):
        s = p = 0.0
        for _ in keys:
            a, b = groups[rng.choice(keys)]
            s += a; p += b
        draws.append(p / s)
    draws.sort()
    return {'matches': len(keys), 'stake': round(stake, 2), 'pnl': round(pnl, 2),
            'roi': round(pnl / stake, 4),
            'ci95': [round(draws[int(.025 * reps)], 4), round(draws[int(.975 * reps)], 4)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--orders', type=Path, default=REPORT / 'data/real_orders_joined.csv',
                        help='Local order CSV, historical archived input by default')
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if not output.is_relative_to(PROJECT) or output.is_relative_to(REPORT / 'data'):
        parser.error('output must stay inside this project and outside fixed archive data')
    import psycopg

    with psycopg.connect(host=os.environ['PG_BASE_URL'], port=int(os.environ['PG_BASE_PORT']),
                         dbname=os.environ['PG_DATASET'], user=os.environ['PG_USER'],
                         password=os.environ['PG_PASSWORD'], connect_timeout=15) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("""select match_id, league, market, odds, p_fused, p_market, pnl from decision_statistics
                where decision='recommendation' and algorithm='economic_ensemble' and competition_type='real'
                and status = any(%s)""", (list(GRADED),))
            rows = cur.fetchall()
    scen = {
        'sim_all_real_labelled': lambda r: True,
        'sim_exclude_8min_virtual': lambda r: '分钟' not in r[1],
        'sim_gap_le_20pp_non_virtual': lambda r: '分钟' not in r[1] and (r[4] - r[5]) <= .20,
        'sim_gap_le_10pp_non_virtual': lambda r: '分钟' not in r[1] and (r[4] - r[5]) <= .10,
        'sim_gap_gt_30pp_non_virtual': lambda r: '分钟' not in r[1] and (r[4] - r[5]) > .30,
    }
    result = {}
    for name, keep in scen.items():
        g = defaultdict(lambda: [0.0, 0.0])
        for r in rows:
            if keep(r):
                g[r[0]][0] += 1; g[r[0]][1] += float(r[6])
        result[name] = boot(g)
    # Real orders, priced at the decision odds of the matched recommendation.
    g = defaultdict(lambda: [0.0, 0.0]); gv = defaultdict(lambda: [0.0, 0.0])
    with args.orders.open(newline='', encoding='utf-8') as f:
        orders = [redact_real_order(r) for r in csv.DictReader(f)]
        for r in orders:
            if r['sim_status'] == 'pending':
                continue
            s = float(r['stake']); p = s * float(r['sim_pnl_per_unit'])
            g[r['match_id']][0] += s; g[r['match_id']][1] += p
            if r['is_virtual'] == 'false' and float(r['p_fused']) - 1 / float(r['decision_odds']) <= .20:
                gv[r['match_id']][0] += s; gv[r['match_id']][1] += p
    result['real_orders_all_graded'] = boot(g)
    result['real_orders_model_minus_inverse_decision_odds_le_20pp_non_virtual'] = boot(gv)
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'real_orders_joined.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=REAL_ORDER_FIELDS, lineterminator='\n')
        writer.writeheader()
        writer.writerows(orders)
    with (output / 'roi_evidence.json').open('w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print('Current PG query plus separately supplied local orders; not the archived snapshot.')
    for k, v in result.items():
        print('%-38s %s' % (k, v))


if __name__ == '__main__':
    sys.exit(main())
