from decimal import Decimal
import unittest

from price_contract import price_contract
from market_reference import price_from_references


def request(**changes):
    data = {
        'contract': {'sport': 'football', 'phase': 'scheduled', 'kind': 'total', 'period': 'full_time',
                     'line': '2.25', 'selection': 'under', 'score_basis': 'full_period'},
        'offered_odds': '2.10',
        'cost_per_unit': '0.01',
        'required_net_edge': '0.02',
        'scenarios': {'base': [{'home': 1, 'away': 1, 'probability': '0.6'},
                               {'home': 2, 'away': 1, 'probability': '0.4'}]},
    }
    return data | changes


class PriceContractTests(unittest.TestCase):
    def test_half_win_and_required_price(self):
        output = price_contract(request())
        self.assertEqual(output['scenarios']['base']['expected_win_fraction'], Decimal('0.30'))
        self.assertEqual(output['worst_net_at_offered_odds'], Decimal('-0.08'))
        self.assertFalse(output['passes_price_test'])
        self.assertTrue(price_contract(request(offered_odds='2.44'))['passes_price_test'])

    def test_worst_distribution_applies(self):
        data = request(offered_odds='2.44')
        data['scenarios']['adverse'] = [{'home': 3, 'away': 1, 'probability': '1'}]
        output = price_contract(data)
        self.assertIsNone(output['minimum_odds'])
        self.assertFalse(output['passes_price_test'])

    def test_live_remaining_score_uses_snapshot(self):
        data = request()
        data['contract'].update(phase='live', score_basis='remaining_period', score_baseline={'home': 1, 'away': 0})
        output = price_contract(data)
        self.assertEqual(output['scenarios']['base']['expected_win_fraction'], Decimal('0.8'))
        self.assertTrue(output['passes_price_test'])

    def test_ambiguous_and_incomplete_inputs_fail(self):
        data = request()
        data['contract'].update(phase='live', score_basis='unknown')
        with self.assertRaises(ValueError):
            price_contract(data)
        data = request()
        data['scenarios']['base'].pop()
        with self.assertRaises(ValueError):
            price_contract(data)

    def test_market_references_price_conservatively(self):
        data = {'line': '2.5', 'direction': 'over', 'offered_odds': '2.05',
                'cost_per_unit': '0.01', 'required_net_edge': '0.02',
                'references': {'a': {'over': '1.8', 'under': '2.0'},
                               'b': {'over': '1.9', 'under': '1.9'}}}
        output = price_from_references(data)
        self.assertEqual(output['minimum_odds'], Decimal('2.06'))
        self.assertFalse(output['passes_price_test'])
        self.assertTrue(price_from_references(data | {'offered_odds': '2.06'})['passes_price_test'])
        data['references'].pop('b')
        with self.assertRaises(ValueError):
            price_from_references(data)


if __name__ == '__main__':
    unittest.main()
