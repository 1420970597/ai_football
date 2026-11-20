"""Evaluate an expert's prior accuracy relative to the quoted difficulty."""

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path

QUERY = """
SELECT s.id,s.fixture_id,s.expert_id,s.created_at,s.withdrawn_at,s.market_kind,s.market_id,s.period,s.selection,
       s.phase,f.sport,s.recommendation_price,ev.evidence::text,
       coalesce(history.versions,'[]'::jsonb)::text AS versions,
       EXISTS (SELECT 1 FROM v2_bet_orders o WHERE o.signal_id=s.id AND NOT o.manual
               AND o.provider_order_no IS NOT NULL AND o.provider_placed_at <= $3) AS native_acceptance
FROM v2_expert_signals s JOIN v2_fixtures f ON f.id=s.fixture_id
LEFT JOIN LATERAL (
    SELECT e.evidence FROM v2_quote_evidence e
    WHERE e.reference_id=s.id AND e.evidence_kind='expert_signal' AND e.captured_at=s.created_at
      AND (e.fixture_id,e.market_id,e.period,e.selection,e.price,e.source_updated_at)
        =(s.fixture_id,s.market_id,s.period,s.selection,s.recommendation_price,s.quote_time)
      AND (e.line IS NOT DISTINCT FROM s.line OR s.market_kind='winner'
           AND coalesce(e.line,0)=0 AND coalesce(s.line,0)=0)
      AND (e.evidence->>'score_basis',e.evidence->>'market_kind')=(s.score_basis,s.market_kind)
      AND e.score_baseline IS NOT DISTINCT FROM s.score_baseline
    ORDER BY e.id LIMIT 1
) ev ON true
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object('version',r.version,'observed_at',r.received_at,'status',r.status,
        'records',coalesce(ledger.records,'[]'::jsonb)) ORDER BY r.version) AS versions
    FROM v2_fixture_results r
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object('computed_at',l.created_at,'outcome',l.outcome,
            'stake',l.effective_stake::text,'net',l.unit_net::text) ORDER BY l.created_at,l.algorithm_version) AS records
        FROM v2_signal_settlements l
        WHERE (l.signal_id,l.fixture_id,l.result_version)=(s.id,r.fixture_id,r.version) AND l.created_at <= $3
    ) ledger ON true
    WHERE r.fixture_id=s.fixture_id AND r.received_at <= $3
) history ON ev.evidence ? 'market_snapshot'
WHERE s.created_at >= $1 AND s.created_at < $2
ORDER BY s.created_at,s.id
"""

ARMS = {'market_adjusted': (5, 20), 'market_adjusted_conservative': (15, 40)}
ACTIVE_OUTCOMES = {'win': Decimal(1), 'half_win': Decimal('0.5'),
                   'loss': Decimal(1), 'half_loss': Decimal('0.5')}
ZERO = Decimal(0)
STRESS_COST = Decimal('0.02')


def utc(value):
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() != timedelta(0):
        raise ValueError('expected UTC timestamp')
    return moment


async def export(begin, end, cutoff):
    import asyncpg
    from app.config import database_url

    conn = await asyncpg.connect(database_url(), timeout=5, command_timeout=15,
                                 server_settings={'default_transaction_read_only': 'on',
                                                  'statement_timeout': '12000', 'TimeZone': 'UTC'})
    try:
        async with conn.transaction(isolation='repeatable_read', readonly=True):
            now = await conn.fetchval('SELECT transaction_timestamp()')
            if not begin < end <= cutoff <= now or cutoff - begin > timedelta(days=120):
                raise ValueError('invalid export interval')
            rows = await conn.fetch(QUERY, begin, end, cutoff)
            return {'begin': begin, 'end': end, 'cutoff': cutoff,
                    'rows': [dict(row) | {'evidence': json.loads(row['evidence'] or '{}'),
                                          'versions': json.loads(row['versions'])} for row in rows]}
    finally:
        await conn.close()


def result_at(pick, cutoff):
    versions = [record for record in pick['versions'] if pick['created_at'] < utc(record['observed_at']) < cutoff]
    if not versions or versions[-1]['status'] not in {'confirmed', 'void'}:
        return None
    version = versions[-1]
    observed = utc(version['observed_at'])
    records = [r for r in version['records'] if observed <= utc(r['computed_at']) < cutoff]
    if not records or records[-1]['net'] is None:
        return None
    row = records[-1]
    outcome, stake, net = row['outcome'], Decimal(row['stake']), Decimal(row['net'])
    if outcome in {'push', 'void'} and stake == 0 and net == 0:
        return ZERO, ZERO
    if outcome not in ACTIVE_OUTCOMES or stake != ACTIVE_OUTCOMES[outcome]:
        return None
    won = stake if outcome.endswith('win') else ZERO
    expected_net = won * pick['recommendation_price'] - stake
    if expected_net.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP) != net:
        return None
    return won, stake


def prepare(row):
    from replay_opposite import verified_prices

    pick = row | {'created_at': utc(row['created_at']), 'recommendation_price': Decimal(row['recommendation_price']),
                  'withdrawn_at': utc(row['withdrawn_at']) if row['withdrawn_at'] else None}
    reason, prices = verified_prices(pick, allow_three_way=True)
    if prices is None:
        return reason, pick
    sides = {side: 1 / price for side, price in prices.items()}
    return 'verified', pick | {'market_share': sides[pick['selection']] / sum(sides.values())}


def past_residual(pick, history):
    games = defaultdict(lambda: [ZERO, ZERO])
    t = pick['created_at']
    start = t - timedelta(days=28)
    for prior in history:
        if not start <= prior['created_at'] < t or prior['fixture_id'] == pick['fixture_id']:
            continue
        if prior['withdrawn_at'] and prior['withdrawn_at'] <= t:
            continue
        outcome = result_at(prior, t)
        if outcome is None or not outcome[1]:
            continue
        won, active = outcome
        game = games[prior['fixture_id']]
        game[0] += won - active * prior['market_share']
        game[1] += active
    return len(games), sum((residual / active for residual, active in games.values()), ZERO)


def summary(picks, start, end, cutoff):
    from infra.profitability.simulate_strategies import HALF_DAY, buckets

    windows = defaultdict(lambda: [0, ZERO])
    net = defaultdict(lambda: ZERO)
    settled = []
    excluded = Counter()
    unaccepted = ZERO
    not_withdrawn = ZERO
    for pick in picks:
        result = result_at(pick, cutoff)
        if result is None:
            excluded['missing_as_of_settlement'] += 1
            continue
        won, active = result
        value = won * pick['recommendation_price'] - active - STRESS_COST
        settled.append(pick)
        if pick['withdrawn_at'] and pick['withdrawn_at'] <= cutoff:
            excluded['subsequent_withdrawal'] += 1
        else:
            not_withdrawn += value
        if not pick['native_acceptance']:
            unaccepted += value
        net[pick['market_kind']] += value
        book = windows[(pick['created_at'] - start) // HALF_DAY]
        book[0] += 1
        book[1] += value
    totals = sum(net.values(), ZERO)
    top = max(sorted(net), key=net.get) if net else None
    return {
        'selected': len(picks), 'settled': len(settled), 'fixtures': len({pick['fixture_id'] for pick in settled}),
        'sports': sorted({pick['sport'] for pick in settled}),
        'settled_without_native_acceptance': sum(not pick['native_acceptance'] for pick in settled),
        'stressed_quote_units': totals, 'stressed_quote_units_without_top_market': totals - net[top] if top else None,
        'stressed_quote_units_without_native_acceptance': unaccepted,
        'stressed_quote_units_without_later_withdrawal': not_withdrawn,
        'settlement_coverage': dict(excluded),
        'quote_time_half_days': [{'begin': left, 'end': right, 'complete': complete, 'settled': windows[index][0],
                                  'stressed_quote_units': windows[index][1]}
                                 for index, (left, right, complete) in enumerate(buckets(start, end, HALF_DAY))],
    }


def replay(document, split):
    start, end, cutoff = (utc(document[key]) for key in ('begin', 'end', 'cutoff'))
    if not start < split < end <= cutoff:
        raise ValueError('split must be inside the frozen evidence interval')
    cohorts = {'earlier': (start, split), 'later': (split, end)}
    picked = {key: {arm: [] for arm in ('follow_all_priced', *ARMS)} for key in cohorts}
    coverage = defaultdict(Counter)
    history = defaultdict(list)
    rows = document['rows']
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('duplicate recommendations in exported evidence')
    for row in sorted(rows, key=lambda item: (utc(item['created_at']), item['id'])):
        status, pick = prepare(row)
        t = pick['created_at']
        if not start <= t < end:
            raise ValueError('recommendation outside frozen interval')
        cohort = 'earlier' if t < split else 'later'
        coverage[cohort][status] += 1
        if status != 'verified' or pick['withdrawn_at'] is not None and pick['withdrawn_at'] <= t:
            continue
        picked[cohort]['follow_all_priced'].append(pick)
        profile = history[pick['expert_id'], pick['sport'], pick['phase']]
        fixtures, residual = past_residual(pick, profile)
        for arm, (minimum, prior) in ARMS.items():
            if fixtures >= minimum:
                share = min(Decimal(1), max(ZERO, pick['market_share'] + residual / (fixtures + prior)))
                if share * pick['recommendation_price'] - 1 > STRESS_COST:
                    picked[cohort][arm].append(pick)
        profile.append(pick)
    return {'scope': 'historical_quote_diagnostic', 'assumed_stress': STRESS_COST,
            'validation_eligible': False, 'coverage': coverage,
            'cohorts': {cohort: {arm: summary(picks, left, right, cutoff)
                                 for arm, picks in picked[cohort].items()}
                        for cohort, (left, right) in cohorts.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='mode', required=True)
    query = commands.add_parser('export')
    for bound in ('begin', 'end', 'cutoff'):
        query.add_argument(bound, type=utc)
    run = commands.add_parser('replay')
    run.add_argument('archive', type=Path)
    run.add_argument('split', type=utc)
    args = parser.parse_args()
    if args.mode == 'export':
        output = asyncio.run(export(args.begin, args.end, args.cutoff))
    else:
        raw = args.archive.read_bytes()
        output = replay(json.loads(raw), args.split) | {'input_sha256': sha256(raw).hexdigest()}
    print(json.dumps(output, default=str, sort_keys=True))


if __name__ == '__main__':
    main()
