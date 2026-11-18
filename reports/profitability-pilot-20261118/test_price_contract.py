from copy import deepcopy
from decimal import Decimal
import unittest

from price_contract import price_contract
from market_reference import price_from_references, price_from_verified_references


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


class VerifiedReferenceTests(unittest.TestCase):
    def setUp(self):
        contract = {'fixture_id': 'fixture-a', 'sport': 'football', 'phase': 'scheduled', 'kind': 'total',
                    'period': 'full_time', 'score_basis': 'full_period', 'line': '2.50'}
        quote = {'contract': contract | {'line': '2.5'}, 'status': 'active',
                 'quoted_at': '2026-11-18T11:48:30Z', 'received_at': '2026-11-18T11:49:00Z'}
        self.data = {'contract': contract | {'selection': 'over'}, 'as_of': '2026-11-18T11:49:00Z',
                     'max_age_seconds': 30, 'offer_venue': 'venue-leyu', 'offer_feed_origin': 'feed-leyu',
                     'offered_odds': '2.05', 'cost_per_unit': '0.01', 'required_net_edge': '0.02',
                     'references': {
                         'a': quote | {'venue': 'venue-a', 'feed_origin': 'feed-a', 'over': '1.8', 'under': '2.0'},
                         'b': quote | {'venue': 'venue-b', 'feed_origin': 'feed-b', 'over': '1.9', 'under': '1.9'},
                     }}

    def test_same_contract_and_bounds_are_admitted(self):
        output = price_from_verified_references(self.data)
        self.assertEqual(output['minimum_odds'], Decimal('2.06'))
        self.assertFalse(output['passes_price_test'])

    def test_same_venue_and_same_origin_are_rejected(self):
        for changes in ({'venue': ' VENUE-A '}, {'feed_origin': 'feed-a'},
                        {'venue': 'venue-leyu'}, {'feed_origin': 'feed-leyu'}):
            with self.subTest(changes=changes):
                data = deepcopy(self.data)
                data['references']['b'].update(changes)
                with self.assertRaisesRegex(ValueError, 'must be distinct'):
                    price_from_verified_references(data)

    def test_wrong_fixture_phase_line_period_or_settlement_is_rejected(self):
        for key, value in (('fixture_id', 'fixture-b'), ('phase', 'live'), ('line', '3.5'),
                           ('period', 'first_half'), ('score_basis', 'remaining_period')):
            with self.subTest(key=key):
                data = deepcopy(self.data)
                quote = data['references']['b']
                quote['contract'] = quote['contract'] | {key: value}
                with self.assertRaisesRegex(ValueError, 'reference contract differs'):
                    price_from_verified_references(data)

    def test_stale_deferred_inverted_or_suspended_quotes_are_rejected(self):
        for changes, message in (
            ({'quoted_at': '2026-11-18T11:48:29.999999Z'}, 'stale or unavailable'),
            ({'received_at': '2026-11-18T11:49:00.000001Z'}, 'stale or unavailable'),
            ({'received_at': '2026-11-18T11:48:29Z'}, 'stale or unavailable'),
            ({'quoted_at': '2026-11-18T11:48:30'}, 'must include a timezone'),
            ({'status': 'suspended'}, 'not active'),
        ):
            with self.subTest(changes=changes):
                data = deepcopy(self.data)
                data['references']['b'].update(changes)
                with self.assertRaisesRegex(ValueError, message):
                    price_from_verified_references(data)


if __name__ == '__main__':
    unittest.main()
