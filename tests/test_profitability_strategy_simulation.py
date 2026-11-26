from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from infra.profitability.simulate_strategies import Fill, Receipt, choose, expert_has_edge, run


START = datetime(2026, 10, 1, tzinfo=timezone.utc)


def fill(placed, number, *, fixture=None, market='total', costs=True, net='1', receipts=None,
         price='2.00', overround='0.04'):
    if receipts is None:
        receipts = [{'observed_at': (placed + timedelta(hours=3)).isoformat(),
                     'settled_at': (placed + timedelta(hours=2)).isoformat(),
                     'stake': '1', 'payout': str(1 + Decimal(net)), 'net': net,
                     'fees': '0.01' if costs else None, 'currency': 'CNY' if costs else None}]
    return {'order_id': str(number), 'fixture_id': fixture or str(number), 'expert_id': 'expert',
            'placed_at': placed.isoformat(), 'market_kind': market, 'period': 'full_time', 'sport': 'football',
            'signal_at': (placed - timedelta(minutes=1)).isoformat(), 'signal_price': '2',
            'preflight_at': (placed - timedelta(seconds=1)).isoformat(),
            'preflight_price': price, 'overround': overround, 'receipts': receipts}


def document(rows, start, end):
    return {'begin': start.isoformat(), 'end': end.isoformat(),
            'cutoff': (end + timedelta(days=1)).isoformat(), 'rows': rows}


class ProfitabilityStrategySimulationTest(unittest.TestCase):
    def test_uses_only_known_receipt_versions_and_distinct_prior_fixtures(self):
        start = START + timedelta(days=3)
        row = fill(start, 'candidate')
        candidate = Fill.parse(row)
        prior = [fill(START + timedelta(minutes=n), n) for n in range(10)]
        for raw in prior:
            original = raw['receipts'][0]
            raw['receipts'].append(original | {'observed_at': row['preflight_at'], 'net': '-1', 'payout': '0'})
        parsed = [Fill.parse(raw) for raw in prior]
        self.assertTrue(expert_has_edge(candidate, parsed))
        later = Fill.parse(fill(start + timedelta(seconds=2), 'later'))
        self.assertFalse(expert_has_edge(later, parsed))
        duplicated = [Fill.parse(raw | {'fixture_id': 'one'}) for raw in prior]
        self.assertFalse(expert_has_edge(candidate, duplicated))

    def test_price_and_fixture_rules_compare_same_native_opportunities(self):
        fills = [
            Fill.parse(fill(START, 'a', fixture='f1', price='1.99')),
            Fill.parse(fill(START + timedelta(minutes=4), 'b', fixture='f1')),
            Fill.parse(fill(START + timedelta(minutes=5), 'c', overround='0.05001')),
            Fill.parse(fill(START + timedelta(minutes=6), 'd', overround='0.05')),
        ]
        selected = choose(fills, START, START + timedelta(days=1))
        self.assertEqual(['a', 'b', 'c', 'd'], [r.order for r in selected['baseline']])
        self.assertEqual(['a', 'c', 'd'], [r.order for r in selected['one_per_fixture']])
        self.assertEqual(['b', 'd'], [r.order for r in selected['price_quality']])

    def test_removes_highest_contribution_and_keeps_empty_calendar_windows(self):
        end = START + timedelta(days=14)
        rows = [fill(START, 1, costs=False), fill(START + timedelta(hours=1), 2, market='handicap', net='-1')]
        result = run(document(rows, START, end), START, end)['arms']['baseline']
        self.assertEqual(Decimal('-0.05'), result['stressed_units'])
        self.assertEqual(Decimal('-1.03'), result['stressed_units_without_top_market'])
        self.assertEqual([2, 0], [week['trades'] for week in result['weekly_placement_cohorts']])
        self.assertIn('retrospective_research', result['validation_blockers'])
        self.assertIn('unverified_costs_or_currency', result['validation_blockers'])

    def test_fee_is_charged_once_when_native_net_already_includes_it(self):
        row = fill(START, 1)['receipts'][0] | {'net': '0.99'}
        receipt = Receipt.parse(row, START)
        self.assertEqual(Decimal('0.97'), receipt.stressed_return)

    def test_push_has_no_stress_cost_and_unknown_currency_fee_is_not_deducted(self):
        receipt = Receipt.parse({
            'observed_at': (START + timedelta(hours=3)).isoformat(),
            'settled_at': (START + timedelta(hours=2)).isoformat(),
            'stake': '1', 'payout': '1', 'net': '-0.1', 'fees': '0.1', 'currency': None,
        }, START)
        self.assertEqual(Decimal('0'), receipt.unit_return)
        self.assertIsNone(receipt.currency)
        self.assertEqual(Decimal('0'), receipt.stressed_return)

    def test_registered_synthetic_positive_series_exercises_all_validation_conditions(self):
        end = START + timedelta(days=28)
        rows = [fill(START + timedelta(days=day, minutes=n), day * 8 + n,
                     market=('total', 'handicap')[n % 2]) for day in range(28) for n in range(8)]
        results = run(document(rows, START, end), START, end, registered_at=START, primary_arm='baseline')['arms']
        result = results['baseline']
        self.assertEqual([], result['validation_blockers'])
        self.assertEqual(224, result['fixtures'])
        self.assertGreater(result['lower_95_roi'], 0)
        self.assertIn('retrospective_research', results['one_per_fixture']['validation_blockers'])
