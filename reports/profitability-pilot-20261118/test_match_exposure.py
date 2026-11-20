from copy import deepcopy
from decimal import Decimal
import unittest

from match_exposure import compare_match_exposure


def request():
    contract = {'fixture_id': 'leyu:1', 'sport': 'football', 'phase': 'scheduled', 'kind': 'total',
                'period': 'full_time', 'score_basis': 'full_period', 'line': '2.75', 'selection': 'over'}
    return {'fixture_id': 'leyu:1', 'orders': [
        {'contract': contract, 'stake': '5', 'cost': '0', 'filled_price': '1.85'},
        {'contract': contract | {'phase': 'live'}, 'stake': '5', 'cost': '0', 'filled_price': '1.48'},
    ], 'results': [
        {'status': 'confirmed', 'home': 2, 'away': 0},
        {'status': 'confirmed', 'home': 2, 'away': 1},
        {'status': 'confirmed', 'home': 3, 'away': 1},
        {'status': 'void'},
    ]}


class MatchExposureTests(unittest.TestCase):
    def test_equivalent_contracts_lose_or_win_together_across_phases(self):
        result = compare_match_exposure(request())
        self.assertEqual(Decimal(10), result['total_stake'])
        self.assertEqual(Decimal(10), result['maximum_loss_in_supplied_results'])
        self.assertEqual(list(map(Decimal, ('-10', '3.325', '6.65', '0'))),
                         [row['net'] for row in result['scenarios']])

    def test_opposite_winner_and_handicap_payoffs_with_costs(self):
        data = request()
        first, second = data['orders']
        first['contract'].update(kind='winner', line=None, selection='home')
        second['contract'].update(kind='handicap', line='0.5', selection='away', line_basis='selection')
        first.update(filled_price='2', cost='0.10')
        second.update(filled_price='2', cost='0.10')
        data['results'] += [{'status': 'confirmed', 'home': 2, 'away': 2},
                            {'status': 'confirmed', 'home': 1, 'away': 2}]
        self.assertEqual(Decimal('0.20'), compare_match_exposure(data)['maximum_loss_in_supplied_results'])

    def test_unverified_contract_and_incomplete_result_are_rejected(self):
        data = request()
        for change, reason in (
            ({'fixture_id': 'leyu:2'}, 'verified terms'),
            ({'score_basis': 'unknown'}, 'verified terms'),
            ({'period': 'first_half'}, 'period_score_missing_or_inconsistent'),
            ({'score_basis': 'remaining_period'}, 'live_score_baseline_missing'),
        ):
            with self.subTest(change=change):
                broken = deepcopy(data)
                broken['orders'][1]['contract'].update(change)
                with self.assertRaisesRegex(ValueError, reason):
                    compare_match_exposure(broken)
