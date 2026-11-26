"""Exploratory league/competition performance analysis for accepted orders.

The input order archive intentionally stays separate from competition metadata.
The latter is supplied as an independently captured fixture projection. This
module measures historical return differences only. It does not assess match
integrity or infer manipulation.
"""

from collections import defaultdict
from decimal import ROUND_FLOOR, Decimal
from hashlib import sha256
import argparse
import json
from math import factorial, prod
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


def _exact_fixture_bootstrap_interval(fixture_returns, low_fraction, high_fraction):
    """Enumerate all fixture-cluster bootstrap compositions for small groups."""
    fixture_ids = sorted(fixture_returns)
    count = len(fixture_ids)
    if count < 5 or count > 7:
        return None

    clusters = [
        (len(fixture_returns[fixture]), sum(fixture_returns[fixture], ZERO))
        for fixture in fixture_ids
    ]
    distribution = []

    def visit(position, remaining, counts):
        if position == count - 1:
            composition = counts + [remaining]
            resampled_orders = sum(
                multiplicity * clusters[index][0]
                for index, multiplicity in enumerate(composition)
            )
            resampled_return = sum(
                (multiplicity * clusters[index][1]
                 for index, multiplicity in enumerate(composition)),
                ZERO,
            )
            weight = factorial(count) // prod(factorial(value) for value in composition)
            distribution.append((resampled_return / resampled_orders, weight))
            return

        for multiplicity in range(remaining + 1):
            visit(position + 1, remaining - multiplicity, counts + [multiplicity])

    visit(0, count, [])
    distribution.sort(key=lambda item: item[0])
    total_resamples = count ** count

    def weighted_quantile(fraction):
        position = Decimal(total_resamples - 1) * fraction
        lower_rank = int(position.to_integral_value(rounding=ROUND_FLOOR))
        upper_rank = min(lower_rank + 1, total_resamples - 1)
        interpolation = position - lower_rank
        lower_value = upper_value = None
        cumulative = 0
        for value, weight in distribution:
            next_cumulative = cumulative + weight
            if lower_value is None and lower_rank < next_cumulative:
                lower_value = value
            if upper_rank < next_cumulative:
                upper_value = value
                break
            cumulative = next_cumulative
        return lower_value + (upper_value - lower_value) * interpolation

    return {
        'low': weighted_quantile(low_fraction),
        'high': weighted_quantile(high_fraction),
        'resamples': total_resamples,
        'compositions': len(distribution),
    }


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
    fixtures_with_competition_id = sum(
        competition_id is not None and competition_id != ''
        for competition_id, _, _ in fixture_metadata.values()
    )
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
        familywise_method = None
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
                    exact_interval = _exact_fixture_bootstrap_interval(
                        fixture_returns, corrected_tail, Decimal(1) - corrected_tail,
                    )
                    if exact_interval is not None:
                        intervals['familywise_95_screen'] = {
                            'low': exact_interval['low'],
                            'high': exact_interval['high'],
                            'bonferroni_groups': total_groups,
                            'method': 'exact_fixture_cluster_bootstrap',
                            'resamples': exact_interval['resamples'],
                            'compositions': exact_interval['compositions'],
                        }
                        familywise_method = 'exact_fixture_cluster_bootstrap'
                    else:
                        intervals['familywise_95_screen'] = {
                            'low': quantile(bootstraps, corrected_tail),
                            'high': quantile(bootstraps, Decimal(1) - corrected_tail),
                            'bonferroni_groups': total_groups,
                            'method': 'monte_carlo_fixture_cluster_bootstrap',
                        }
                        familywise_method = 'monte_carlo_fixture_cluster_bootstrap'

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
            'market_mix': _market_summary([(fill, receipt) for _, fill, receipt in trades]),
            'fixture_cluster_intervals': intervals,
            'familywise_interval_method': familywise_method,
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
        'competition_identity': {
            'metadata_source': metadata.get('source'),
            'metadata_fixtures': len(fixture_metadata),
            'fixtures_with_non_null_competition_id': fixtures_with_competition_id,
            'grouping_rule': 'competition_id, competition_name, sport; null IDs group by display name and sport',
        },
        'overall': {
            'gross_unit_return_per_settled_order': total_gross / len(all_settled) if all_settled else None,
            'stress_units': total_stress,
            'stress_roi_per_settled_order': total_stress / len(all_settled) if all_settled else None,
            'market_mix': _market_summary([(fill, receipt) for _, fill, receipt in market_rows]),
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
