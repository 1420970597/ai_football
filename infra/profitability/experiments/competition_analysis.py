"""Exploratory league/competition performance analysis for accepted orders.

The input order archive intentionally stays separate from competition metadata.
The latter is supplied as an independently captured fixture projection. This
module measures historical return differences only. It does not assess match
integrity or infer manipulation.
"""

from collections import defaultdict
from decimal import Decimal
from hashlib import sha256
import argparse
import json
from pathlib import Path
from random import Random

from infra.profitability.simulate_strategies import Fill, utc
from .stats import quantile

ZERO = Decimal(0)
SEED = 20261126
DEFAULT_ROUNDS = 100_000
MIN_CORRECTED_SAMPLE = 10


def _fixture_map(document):
    if not isinstance(document, dict) or not isinstance(document.get('fixtures'), list):
        raise ValueError('fixture metadata requires a fixtures array')
    result = {}
    for item in document['fixtures']:
        if not isinstance(item, dict):
            raise ValueError('fixture metadata entries must be objects')
        fixture = item.get('fixture_id')
        competition = item.get('competition_name')
        sport = item.get('sport')
        if not isinstance(fixture, str) or not fixture or not isinstance(competition, str) or not competition:
            raise ValueError('fixture metadata requires fixture_id and competition_name')
        if not isinstance(sport, str) or not sport:
            raise ValueError('fixture metadata requires sport')
        record = (item.get('competition_id'), competition, sport)
        previous = result.get(fixture)
        if previous is not None and previous != record:
            raise ValueError('conflicting competition metadata for fixture')
        result[fixture] = record
    return result


def _bootstrap_fixture_means(fixture_returns, rounds, seed):
    fixture_ids = sorted(fixture_returns)
    count = len(fixture_ids)
    if count < 5:
        return None
    rng = Random(seed)
    means = []
    for _ in range(rounds):
        sampled = [fixture_returns[fixture_ids[rng.randrange(count)]] for _ in range(count)]
        bet_count = sum(len(values) for values in sampled)
        if bet_count:
            total = sum((sum(values, ZERO) for values in sampled), ZERO)
            means.append(total / bet_count)
    return means


def _market_summary(rows):
    groups = defaultdict(list)
    for row, receipt in rows:
        groups[row.market].append(receipt.stressed_return)
    return {
        market: {
            'settled': len(values),
            'stress_units': sum(values, ZERO),
            'stress_roi_per_unit': sum(values, ZERO) / len(values),
        }
        for market, values in sorted(groups.items())
    }


def analyze(cohort, metadata, *, bootstrap_rounds=DEFAULT_ROUNDS):
    if not isinstance(cohort, dict) or not isinstance(cohort.get('rows'), list):
        raise ValueError('cohort requires a rows array')
    if bootstrap_rounds < 100:
        raise ValueError('bootstrap_rounds must be at least 100')
    cutoff = utc(cohort['cutoff'])
    fixture_metadata = _fixture_map(metadata)
    rows = cohort['rows']
    fills = [Fill.parse(row) for row in rows]
    order_ids = [fill.order for fill in fills]
    if len(order_ids) != len(set(order_ids)):
        raise ValueError('duplicate order_id in cohort')

    competitions = defaultdict(list)
    unmatched_orders = 0
    unresolved_orders = 0
    all_settled = []
    for row, fill in zip(rows, fills, strict=True):
        receipt = fill.latest_receipt(cutoff)
        if receipt is None:
            unresolved_orders += 1
        else:
            all_settled.append((row, fill, receipt))
        competition = fixture_metadata.get(fill.fixture)
        if competition is None:
            unmatched_orders += 1
            continue
        competitions[competition].append((row, fill, receipt))

    mapped_trades = [
        (row, fill, receipt)
        for orders in competitions.values()
        for row, fill, receipt in orders
        if receipt is not None
    ]
    total_groups = len(competitions)
    corrected_tail = Decimal('0.025') / max(total_groups, 1)
    expert_totals = defaultdict(list)
    for _, fill, receipt in mapped_trades:
        expert_totals[fill.expert].append(receipt.stressed_return)
    results = []
    for key, orders in competitions.items():
        competition_id, competition_name, sport = key
        trades = [(row, fill, receipt) for row, fill, receipt in orders if receipt is not None]
        fixture_returns = defaultdict(list)
        expert_returns = defaultdict(list)
        positive = negative = push = 0
        missing_fees = missing_currency = 0
        for row, fill, receipt in trades:
            fixture_returns[fill.fixture].append(receipt.stressed_return)
            expert_returns[fill.expert].append(receipt.stressed_return)
            positive += int(receipt.unit_return > ZERO)
            negative += int(receipt.unit_return < ZERO)
            push += int(receipt.unit_return == ZERO)
            missing_fees += int(receipt.unit_fees is None)
            missing_currency += int(receipt.currency is None)

        settled = len(trades)
        stress_units = sum((receipt.stressed_return for _, _, receipt in trades), ZERO)
        gross_units = sum((receipt.unit_return for _, _, receipt in trades), ZERO)
        bootstraps = None
        intervals = None
        if settled:
            bootstraps = _bootstrap_fixture_means(
                fixture_returns, bootstrap_rounds, SEED + len(results),
            )
            if bootstraps is not None:
                intervals = {
                    'fixture_cluster_95': {
                        'low': quantile(bootstraps, Decimal('0.025')),
                        'high': quantile(bootstraps, Decimal('0.975')),
                    },
                    'familywise_95_screen': None,
                }
                if settled >= MIN_CORRECTED_SAMPLE:
                    intervals['familywise_95_screen'] = {
                        'low': quantile(bootstraps, corrected_tail),
                        'high': quantile(bootstraps, Decimal(1) - corrected_tail),
                        'bonferroni_groups': total_groups,
                    }

        other_experts = {
            expert: values for expert, values in expert_totals.items()
        }
        paired = []
        for expert, values in expert_returns.items():
            outside = [value for value in other_experts.get(expert, [])]
            outside_count = len(outside) - len(values)
            if len(values) >= 2 and outside_count >= 3:
                # Remove this competition's rows from the cross-competition total.
                outside_total = sum(outside, ZERO) - sum(values, ZERO)
                paired.append((sum(values, ZERO) / len(values), outside_total / outside_count))

        results.append({
            'competition_id': competition_id,
            'competition_name': competition_name,
            'sport': sport,
            'accepted_orders': len(orders),
            'settled_orders': settled,
            'unresolved_orders': len(orders) - settled,
            'fixtures': len(fixture_returns),
            'experts': len(expert_returns),
            'wins': positive,
            'losses': negative,
            'pushes': push,
            'gross_unit_return_per_settled_order': gross_units / settled if settled else None,
            'stress_units': stress_units,
            'stress_roi_per_settled_order': stress_units / settled if settled else None,
            'missing_fee_orders': missing_fees,
            'missing_currency_orders': missing_currency,
            'market_mix': _market_summary(trades),
            'fixture_cluster_intervals': intervals,
            'same_expert_comparison': {
                'paired_experts': len(paired),
                'league_roi_lower_than_same_experts_outside': sum(
                    in_competition < outside for in_competition, outside in paired
                ),
            },
        })

    results.sort(key=lambda item: (-item['settled_orders'], item['competition_name']))
    total_stress = sum((receipt.stressed_return for _, _, receipt in all_settled), ZERO)
    total_gross = sum((receipt.unit_return for _, _, receipt in all_settled), ZERO)
    market_rows = [(row, fill, receipt) for row, fill, receipt in all_settled]
    return {
        'study': 'exploratory_competition_performance',
        'integrity_assessment': 'not_assessed_from_returns',
        'window': {
            'begin': cohort['begin'], 'end': cohort['end'], 'cutoff': cohort['cutoff'],
        },
        'coverage': {
            'accepted_orders': len(rows),
            'distinct_fixtures': len({fill.fixture for fill in fills}),
            'metadata_fixtures_matched': len({fill.fixture for fill in fills
                                              if fill.fixture in fixture_metadata}),
            'competition_groups': total_groups,
            'unmatched_orders': unmatched_orders,
            'settled_orders': len(all_settled),
            'unresolved_orders': unresolved_orders,
            'fees_missing': sum(receipt.unit_fees is None for _, _, receipt in all_settled),
            'currency_missing': sum(receipt.currency is None for _, _, receipt in all_settled),
        },
        'overall': {
            'gross_unit_return_per_settled_order': total_gross / len(all_settled) if all_settled else None,
            'stress_units': total_stress,
            'stress_roi_per_settled_order': total_stress / len(all_settled) if all_settled else None,
            'market_mix': _market_summary([(row, receipt) for row, _, receipt in market_rows]),
        },
        'inference': {
            'bootstrap_unit': 'fixture',
            'bootstrap_rounds': bootstrap_rounds,
            'seed': SEED,
            'familywise_screen_tail': corrected_tail,
            'interpretation': 'performance screen only; no manipulation classification',
        },
        'competitions': results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cohort', type=Path)
    parser.add_argument('fixture_metadata', type=Path)
    parser.add_argument('--bootstrap-rounds', type=int, default=DEFAULT_ROUNDS)
    args = parser.parse_args()
    cohort_bytes = args.cohort.read_bytes()
    metadata_bytes = args.fixture_metadata.read_bytes()
    result = analyze(
        json.loads(cohort_bytes), json.loads(metadata_bytes),
        bootstrap_rounds=args.bootstrap_rounds,
    )
    result['input_sha256'] = {
        'cohort': sha256(cohort_bytes).hexdigest(),
        'fixture_metadata': sha256(metadata_bytes).hexdigest(),
    }
    print(json.dumps(result, default=str, sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
