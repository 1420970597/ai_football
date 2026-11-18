"""Replay opposite directions at the original immutable two-sided quote."""

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import json

import asyncpg

from app.config import database_url
from app.expert_statistics import QUOTE_VALID_SQL, SETTLEMENT_VALID_SQL
from app.team_totals import team_total_terms


def observed_returns(row):
    status, prices = verified_prices(row, allow_three_way=False)
    if prices is None:
        return status, None
    selection = row['selection']
    opposite, = prices.keys() - {selection}
    if not row['settlement_valid'] or row['outcome'] not in {
            'win', 'half_win', 'loss', 'half_loss', 'push', 'void'}:
        return 'settlement_unavailable', None

    opposite_return = Decimal(0)
    if row['outcome'] in {'win', 'half_win'}:
        opposite_return = -row['effective_stake']
    elif row['outcome'] in {'loss', 'half_loss'}:
        opposite_return = row['effective_stake'] * (prices[opposite] - 1)
    return 'comparable', (row['unit_net'], opposite_return, row['unit_net'] + opposite_return, row['effective_stake'])


def verified_prices(row, *, allow_three_way):
    kind = row['market_kind']
    selection = row['selection']
    if kind in {'total', 'team_total'}:
        sides = {'over', 'under'}
    elif kind == 'handicap':
        if row['evidence'].get('line_basis') != 'provider_home':
            return 'line_basis_unverified', None
        sides = {'home', 'away'}
    elif (kind, row['sport'], row['period'], row['market_id']) == ('winner', 'basketball', 'full_time', '37'):
        sides = {'home', 'away'}
    elif allow_three_way and (kind, row['sport']) == ('winner', 'football'):
        sides = {'home', 'draw', 'away'}
    else:
        return 'not_a_verified_two_way_market', None
    if selection not in sides:
        return 'not_a_verified_two_way_market', None

    prefix = 'two_sided' if len(sides) == 2 else 'three_way'
    snapshot = row['evidence'].get('market_snapshot') or {}
    if not isinstance(snapshot, dict):
        return prefix + '_snapshot_invalid', None
    options = snapshot.get('options')
    if not isinstance(options, list) or len(options) != len(sides):
        return prefix + '_snapshot_unavailable', None
    if kind == 'team_total':
        terms = team_total_terms(dict(row, play_id=snapshot.get('play_id')), row['fixture_id'])
        if (row['sport'] != 'football' or not terms or terms['team_side'] != row['evidence'].get('team_side')
                or not snapshot.get('play_id') or not snapshot.get('market_instance_id')):
            return 'team_contract_unverified', None
    try:
        prices = {option['selection']: Decimal(option['price']) for option in options}
        identities = {option['selection_id'] for option in options if option['selection_id']}
    except (KeyError, TypeError, ArithmeticError, ValueError):
        return prefix + '_snapshot_invalid', None
    if (prices.keys() != sides or len(identities) != len(sides)
            or any(not price.is_finite() or price <= 1 for price in prices.values())
            or prices[selection] != row['recommendation_price']):
        return prefix + '_snapshot_invalid', None
    return 'verified', prices


def summarize(rows, split, *, by_contract=False):
    groups = defaultdict(lambda: defaultdict(int))
    fixtures = defaultdict(set)
    dimensions = ('phase', 'sport', 'market_kind', 'period') if by_contract else ('phase',)
    for row in rows:
        window = 'earlier' if row['created_at'] < split else 'later'
        row = dict(row, evidence=json.loads(row['evidence'] or '{}'))
        status, values = observed_returns(row)
        keys = [tuple(row[name] for name in dimensions)] if by_contract else [('all',), (row['phase'],)]
        for key in keys:
            group = groups[(window, key)]
            group[status] += 1
            if values is not None:
                fixtures[(window, key)].add(row['fixture_id'])
                for metric, value in zip(('follow_units', 'opposite_units', 'paired_units', 'effective_stake'), values):
                    group[metric] += value
    return [{'window': window, **dict(zip(dimensions, key)),
             'comparable_fixtures': len(fixtures[(window, key)]), **values}
            for (window, key), values in sorted(groups.items())]


def summarize_calibration(rows, split):
    groups = defaultdict(lambda: defaultdict(int))
    fixtures = defaultdict(lambda: defaultdict(lambda: defaultdict(Decimal)))
    confidence_groups = defaultdict(lambda: defaultdict(Decimal))
    for row in rows:
        row = dict(row, evidence=json.loads(row['evidence'] or '{}'))
        window = 'earlier' if row['created_at'] < split else 'later'
        keys = [(window, 'all', None, None, None), (window, row['phase'], None, None, None),
                (window, row['phase'], row['sport'], row['market_kind'], row['period'])]
        status, prices = verified_prices(row, allow_three_way=True)
        if prices is not None:
            outcome = row['outcome']
            effective = row['effective_stake']
            if not row['settlement_valid'] or outcome not in {'win', 'half_win', 'loss', 'half_loss', 'push', 'void'}:
                status = 'settlement_unavailable'
            elif outcome in {'push', 'void'}:
                status = 'no_active_stake'
            elif effective != (Decimal('0.5') if outcome.startswith('half_') else Decimal(1)):
                status = 'settlement_stake_inconsistent'
            else:
                status = 'scored'
                won = int(outcome in {'win', 'half_win'})
                fair = (1 / prices[row['selection']]) / sum(1 / price for price in prices.values())
                neutral = Decimal(1) / len(prices)
                confidence = confidence_groups[(window, row['phase'], row['confidence'])]
                confidence['scored'] += 1
                confidence['active_stake'] += effective
                confidence['winning_stake'] += won * effective
                for key in keys:
                    value = fixtures[key][row['fixture_id']]
                    value['active_stake'] += effective
                    value['observed_win_share'] += won * effective
                    value['market_win_share'] += fair * effective
                    value['market_brier'] += (fair - won)**2 * effective
                    value['neutral_brier'] += (neutral - won)**2 * effective
        for key in keys:
            groups[key][status] += 1

    summaries = []
    for key, counts in groups.items():
        window, phase, sport, kind, period = key
        by_fixture = fixtures[key].values()
        metrics = {}
        if by_fixture:
            for metric in ('observed_win_share', 'market_win_share', 'market_brier', 'neutral_brier'):
                metrics[metric] = sum(v[metric] / v['active_stake'] for v in by_fixture) / len(by_fixture)
        summaries.append({'window': window, 'phase': phase, 'sport': sport, 'market_kind': kind, 'period': period,
                          'settled_fixtures': len(by_fixture), **counts, **metrics})
    confidence = [{'window': window, 'phase': phase, 'review_confidence': value, **counts}
                  for (window, phase, value), counts in sorted(confidence_groups.items())]
    return {'groups': summaries, 'review_confidence_distribution': confidence}


async def replay(begin, split, end):
    connection = await asyncpg.connect(database_url(), timeout=5, command_timeout=12)
    try:
        async with connection.transaction(isolation='repeatable_read', readonly=True):
            now = await connection.fetchval('SELECT now()')
            rows = await connection.fetch(f"""
                SELECT s.created_at, s.fixture_id, s.phase, s.market_kind, s.market_id, s.period, s.selection, f.sport,
                       s.recommendation_price, s.confidence, s.outcome, s.unit_net, s.effective_stake, q.evidence,
                       ({SETTLEMENT_VALID_SQL}) AS settlement_valid
                FROM v2_expert_signals s JOIN v2_fixtures f ON f.id=s.fixture_id
                LEFT JOIN LATERAL (
                    SELECT evidence FROM v2_quote_evidence ev
                    WHERE ev.reference_id=s.id AND ev.evidence_kind='expert_signal' AND ev.fixture_id=s.fixture_id
                      AND ev.market_id=s.market_id AND ev.period=s.period AND ev.selection=s.selection
                      AND ev.line IS NOT DISTINCT FROM s.line AND ev.price=s.recommendation_price
                      AND ev.source_updated_at=s.quote_time AND ev.score_baseline IS NOT DISTINCT FROM s.score_baseline
                      AND ev.evidence->>'market_kind'=s.market_kind AND ev.evidence->>'score_basis'=s.score_basis
                      AND ev.captured_at=s.created_at
                    ORDER BY ev.captured_at,ev.id LIMIT 1
                ) q ON true
                WHERE s.created_at >= $1 AND s.created_at < least($2, now())
                  AND s.withdrawn_at IS NULL AND ({QUOTE_VALID_SQL})
                ORDER BY s.created_at,s.id
                """, begin, end)
            return {'begin': begin, 'split': split, 'end': end, 'observed_at': now,
                    'groups': summarize(rows, split), 'contract_groups': summarize(rows, split, by_contract=True),
                    'calibration': summarize_calibration(rows, split)}
    finally:
        await connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for label in ('begin', 'split', 'end'):
        parser.add_argument(label, type=datetime.fromisoformat)
    args = parser.parse_args()
    dates = args.begin, args.split, args.end
    if any(value.utcoffset() is None for value in dates) or not args.begin < args.split < args.end:
        parser.error('Expected three increasing timestamps with timezones')
    print(json.dumps(asyncio.run(replay(*(value.astimezone(timezone.utc) for value in dates))), default=str))
