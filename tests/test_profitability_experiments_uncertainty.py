import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from infra.profitability.experiments.evidence import INSUFFICIENT, MEASURED
from infra.profitability.experiments.uncertainty import economics, resampling

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = START + timedelta(days=16)


def row(placed, number, *, fixture=None, net='1', fees='0.01', currency='CNY',
        settled=None, observed=None):
    settled = settled or placed + timedelta(hours=2)
    observed = observed or placed + timedelta(hours=3)
    return {
        'order_id': str(number),
        'fixture_id': fixture or str(number),
        'expert_id': 'expert',
        'placed_at': placed.isoformat(),
        'market_kind': 'total',
        'period': 'full_time',
        'sport': 'football',
        'signal_at': (placed - timedelta(minutes=1)).isoformat(),
        'signal_price': '2',
        'preflight_at': (placed - timedelta(seconds=1)).isoformat(),
        'preflight_price': '2.00',
        'overround': '0.04',
        'receipts': [{
            'observed_at': observed.isoformat(),
            'settled_at': settled.isoformat(),
            'stake': '1',
            'payout': str(Decimal(1) + Decimal(net)),
            'net': net,
            'fees': fees,
            'currency': currency,
        }],
    }


def document(rows, *, capacity=None, expenses=None, payout_ceiling=None, end=END):
    result = {'begin': START.isoformat(), 'end': end.isoformat(),
              'cutoff': (end + timedelta(days=1)).isoformat(), 'rows': rows}
    if capacity is not None:
        result['capacity'] = capacity
    if expenses is not None:
        result['expense_scenarios'] = expenses
    if payout_ceiling is not None:
        result['payout_ceiling'] = payout_ceiling
    return result


class EconomicsTest(unittest.TestCase):
    def test_lists_each_independent_gap_and_keeps_frozen_evaluator_consistent(self):
        rows = [
            row(START + timedelta(hours=1), 1, fixture='f1'),
            row(START + timedelta(hours=2), 2, fixture='f2', fees=None, currency=None),
        ]
        result = economics(document(rows))
        self.assertEqual(INSUFFICIENT, result['evidence_label'])
        self.assertEqual(2, result['receipts']['settled'])
        self.assertEqual(1, result['receipts']['missing_fees'])
        self.assertEqual(1, result['receipts']['missing_currency'])
        self.assertEqual('not_met', result['gaps']['missing_fees']['state'])
        self.assertEqual('not_met', result['gaps']['missing_currency']['state'])
        self.assertEqual('reported', result['gaps']['maximum_drawdown']['state'])
        self.assertIn('units', result['gaps']['maximum_drawdown'])
        self.assertEqual('not_met', result['gaps']['fewer_than_200_fixtures']['state'])
        self.assertEqual('not_met', result['gaps']['four_positive_complete_weeks']['state'])
        self.assertTrue(result['frozen_evaluator']['consistent_with_recount'])
        self.assertEqual(result['receipts']['selected'] - 2,
                         result['gaps']['unsettled_receipts']['unsettled'])

    def test_missing_fees_block_economic_acceptance_even_with_positive_returns(self):
        rows = [row(START + timedelta(hours=1), 1, fees=None)]
        result = economics(document(rows))
        self.assertEqual(INSUFFICIENT, result['evidence_label'])
        self.assertNotEqual(MEASURED, result['evidence_label'])
        self.assertEqual('not_met', result['gaps']['missing_fees']['state'])
        self.assertEqual(1, result['gaps']['missing_fees']['missing'])
        self.assertGreater(result['returns']['stressed_units'], 0)

    def test_missing_capacity_is_a_labelled_scenario_and_never_zero_filled(self):
        rows = [row(START + timedelta(hours=1), 1), row(START + timedelta(hours=2), 2)]
        result = economics(document(rows))
        self.assertEqual('missing', result['capacity']['status'])
        self.assertIsNone(result['capacity']['units'])
        self.assertEqual('unknown', result['payout_ceiling']['status'])
        self.assertFalse(result['payout_ceiling']['fabricated'])
        self.assertTrue(result['sensitivity']['results_are_scenarios'])
        self.assertTrue(result['sensitivity']['rows'])
        for item in result['sensitivity']['rows']:
            self.assertTrue(item['capacity_basis'].startswith('scenario_'))
            self.assertGreater(item['capacity_units'], 0)
            self.assertEqual('scenario', item['expense_basis'])
        marginal = result['returns']['stressed_roi']
        first = result['sensitivity']['rows'][0]
        self.assertEqual(Decimal(2) * marginal, first['contribution_units'])

    def test_evidenced_capacity_and_expense_produce_testable_break_even(self):
        rows = [row(START + timedelta(hours=1), 1), row(START + timedelta(hours=2), 2)]
        result = economics(document(
            rows,
            capacity={'units': '100', 'source': 'depth snapshot'},
            expenses=[{'units': '5', 'source': 'invoice'}],
            payout_ceiling={'units': '1000', 'source': 'settlement rules'},
        ))
        self.assertEqual('reported', result['capacity']['status'])
        self.assertEqual(Decimal('100'), result['capacity']['units'])
        self.assertEqual('reported', result['payout_ceiling']['status'])
        self.assertEqual(MEASURED, result['sensitivity']['evidence_label'])
        self.assertFalse(result['sensitivity']['results_are_scenarios'])
        item = result['sensitivity']['rows'][0]
        self.assertEqual(Decimal('100'), item['capacity_units'])
        self.assertEqual(Decimal('0.05'), item['break_even_marginal_return'])
        self.assertEqual(Decimal('100') * result['returns']['stressed_roi'] - Decimal('5'),
                         item['net_after_assumed_expense'])
        self.assertTrue(item['profitable_under_assumption'])


class ResamplingTest(unittest.TestCase):
    def test_three_resamplings_keep_fixture_legs_together_and_are_reproducible(self):
        rows = []
        number = 0
        for index in range(4):
            fixture = f'f{index}'
            for leg in range(2):
                number += 1
                rows.append(row(START + timedelta(days=index * 2, hours=leg),
                                number, fixture=fixture, net=('1', '-1', '0.5', '-0.5')[index]))
        document_value = document(rows)
        first = resampling(document_value)
        second = resampling(document_value)
        self.assertEqual(MEASURED, first['evidence_label'])
        self.assertEqual(8, first['settled'])
        self.assertEqual(4, first['clusters']['fixtures'])
        self.assertEqual({2: 4}, first['clusters']['fixture_leg_counts'])
        self.assertTrue(first['clusters']['fixture_legs_kept_together'])
        for key in ('per_bet', 'per_fixture', 'two_day_block'):
            interval = first['intervals'][key]
            self.assertIsNotNone(interval)
            self.assertLessEqual(interval['low'], interval['high'])
        self.assertEqual(20261122, first['seed'])
        self.assertEqual(first, second)
        identification = first['identification_2pp']
        self.assertEqual(Decimal('0.02'), identification['margin'])
        self.assertIsNotNone(identification['independent_bets'])
        self.assertIn('independent', identification['assumption'])

    def test_empty_sample_and_degenerate_variance_are_recorded(self):
        empty = resampling(document([]))
        self.assertEqual(INSUFFICIENT, empty['evidence_label'])
        self.assertEqual(['empty_settled_sample'], empty['missing_reasons'])
        self.assertIsNone(empty['intervals']['per_bet'])

        rows = [row(START + timedelta(hours=1), 1, fixture='f1', net='0.5', fees='0'),
                row(START + timedelta(hours=2), 2, fixture='f1', net='0.5', fees='0'),
                row(START + timedelta(hours=3), 3, fixture='f2', net='0.5', fees='0')]
        degenerate = resampling(document(rows))
        self.assertTrue(degenerate['degenerate_variance'])
        self.assertEqual(['degenerate_variance'], degenerate['missing_reasons'])
        self.assertIsNone(degenerate['design_effect']['per_fixture'])
        self.assertEqual(degenerate['intervals']['per_bet']['low'],
                         degenerate['intervals']['per_bet']['high'])


if __name__ == '__main__':
    unittest.main()
