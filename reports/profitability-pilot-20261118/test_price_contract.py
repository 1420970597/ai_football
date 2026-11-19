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
                     'offer_status': 'active', 'offer_quoted_at': '2026-11-18T11:48:30Z',
                     'offer_received_at': '2026-11-18T11:49:00Z',
                     'offered_odds': '2.05', 'cost_per_unit': '0.01', 'required_net_edge': '0.02',
                     'references': {
                         'a': quote | {'venue': 'venue-a', 'feed_origin': 'feed-a', 'over': '1.8', 'under': '2.0'},
                         'b': quote | {'venue': 'venue-b', 'feed_origin': 'feed-b', 'over': '1.9', 'under': '1.9'},
                     }}

    def test_same_contract_and_bounds_are_admitted(self):
        output = price_from_verified_references(self.data)
        self.assertEqual(output['minimum_odds'], Decimal('2.06'))
        self.assertFalse(output['passes_price_test'])

    def test_new_scheduled_markets_require_fresh_offered_quote(self):
        offer_keys = {'offer_status', 'offer_quoted_at', 'offer_received_at'}
        data = {key: value for key, value in deepcopy(self.data).items() if key not in offer_keys}
        self.assertEqual(price_from_verified_references(data)['minimum_odds'], Decimal('2.06'))
        self.replace_contract(data, line='2.25')
        with self.assertRaises(KeyError):
            price_from_verified_references(data)
        data.update(offer_status='active', offer_quoted_at='2026-11-18T11:48:29Z',
                    offer_received_at='2026-11-18T11:49:00Z')
        with self.assertRaisesRegex(ValueError, 'stale or unavailable'):
            price_from_verified_references(data)

    def replace_contract(self, document, **changes):
        document['contract'].update(changes)
        changes = {key: value for key, value in changes.items() if key != 'selection'}
        for quote in document['references'].values():
            quote['contract'].update(changes)
        return document

    def test_quarter_line_and_integer_line_use_different_action_bounds(self):
        data = self.replace_contract(deepcopy(self.data), line='2.25')
        data['offered_odds'] = '2.10'
        result = price_from_verified_references(data)
        self.assertEqual(result['minimum_odds'], Decimal('2.12'))
        self.assertEqual(result['worst_net_at_offered_odds'], Decimal('0.015'))
        self.assertFalse(result['passes_price_test'])
        self.assertEqual(price_from_verified_references(data | {'offered_odds': '1.90'})['worst_net_at_offered_odds'],
                         Decimal('-0.06'))

        self.replace_contract(data, line='2')
        gap = price_from_verified_references(data | {'offered_odds': '100'})
        self.assertIsNone(gap['minimum_odds'])
        self.assertFalse(gap['passes_price_test'])
        integer = price_from_verified_references(data | {'active_stake_lower_bound': '0.75', 'offered_odds': '2.08'})
        self.assertEqual(integer['minimum_odds'], Decimal('2.08'))
        self.assertTrue(integer['passes_price_test'])
        with self.assertRaisesRegex(ValueError, 'active stake lower bound'):
            price_from_verified_references(data | {'active_stake_lower_bound': '1.01'})

    def test_three_way_requires_draw_price(self):
        data = self.replace_contract(deepcopy(self.data), kind='winner', line=None, selection='draw')
        for quote in data['references'].values():
            quote.update(home='2', draw='4', away='4')
        data['offered_odds'] = '4.12'
        result = price_from_verified_references(data)
        self.assertEqual(result['minimum_odds'], Decimal('4.12'))
        self.assertTrue(result['passes_price_test'])
        data['references']['a'].pop('draw')
        with self.assertRaises(KeyError):
            price_from_verified_references(data)

    def test_remaining_handicap_requires_same_home_line_and_score_basis(self):
        data = self.replace_contract(self.live_request('second_half'), kind='handicap', line='-0.75',
                                     line_basis='provider_home', score_basis='remaining_period', selection='away')
        for quote in data['references'].values():
            quote.update(home='1.9', away='1.9')
        self.assertEqual(price_from_verified_references(data)['minimum_odds'], Decimal('2.12'))
        data['references']['b']['contract']['line'] = '0.75'
        with self.assertRaisesRegex(ValueError, 'contract differs'):
            price_from_verified_references(data)

    def test_team_total_checks_which_team_has_scored(self):
        data = self.replace_contract(self.live_request(), kind='team_total', team_side='home', line='1.5')
        self.assertEqual(price_from_verified_references(data)['minimum_odds'], Decimal('2.06'))
        data['references']['a']['contract']['team_side'] = 'away'
        with self.assertRaisesRegex(ValueError, 'contract differs'):
            price_from_verified_references(data)

    def test_basketball_requires_same_settlement_scope(self):
        data = self.replace_contract(deepcopy(self.data), sport='basketball', line='208.5',
                                     settlement_scope='including_overtime')
        self.assertEqual(price_from_verified_references(data)['minimum_odds'], Decimal('2.06'))
        data['references']['a']['contract']['settlement_scope'] = 'regulation'
        with self.assertRaisesRegex(ValueError, 'contract differs'):
            price_from_verified_references(data)

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

    def live_request(self, period='first_half'):
        data = deepcopy(self.data)
        state = {'period': period, 'status': 'playing', 'home_score': 1, 'away_score': 1,
                 'home_red_cards': 0, 'away_red_cards': 0, 'confirmed_at': '2026-11-18T11:48:29Z',
                 'received_at': '2026-11-18T11:48:30Z'}
        contract_period = 'first_half' if period == 'first_half' else 'full_time'
        data.update(event_state=state, offer_status='active', offer_quoted_at='2026-11-18T11:48:31Z',
                    offer_received_at='2026-11-18T11:48:32Z')
        data['contract'].update(phase='live', period=contract_period)
        for quote in data['references'].values():
            quote['contract'].update(phase='live', period=contract_period)
            quote['event_state'] = dict(state)
        return data

    def test_live_periods_preserve_the_same_binary_price(self):
        for period in ('first_half', 'second_half'):
            with self.subTest(period=period):
                output = price_from_verified_references(self.live_request(period))
                self.assertEqual(output['minimum_odds'], Decimal('2.06'))
                self.assertFalse(output['passes_price_test'])

    def test_live_event_mismatch_or_unavailable_confirmation_is_rejected(self):
        for changes, reason in (
            ({'status': 'pending_confirmation'}, 'playing and confirmed'),
            ({'period': 'second_half'}, 'playing and confirmed'),
            ({'away_score': 2}, 'live state differs'),
            ({'home_red_cards': 1}, 'live state differs'),
            ({'away_score': True}, 'nonnegative integers'),
            ({'received_at': '2026-11-18T11:49:01Z'}, 'state was unavailable'),
            ({'confirmed_at': '2026-11-18T11:48:31Z', 'received_at': '2026-11-18T11:48:31Z'}, 'quote predates'),
        ):
            with self.subTest(changes=changes):
                data = self.live_request()
                data['references']['b']['event_state'].update(changes)
                with self.assertRaisesRegex(ValueError, reason):
                    price_from_verified_references(data)

    def test_live_state_snapshot_must_be_recent_even_when_quotes_are_fresh(self):
        for stale_offer in (True, False):
            with self.subTest(stale_offer=stale_offer):
                data = self.live_request()
                stale = data['event_state'] if stale_offer else data['references']['b']['event_state']
                stale['received_at'] = '2026-11-18T11:48:29.999999Z'
                with self.assertRaisesRegex(ValueError, 'live state observation is stale'):
                    price_from_verified_references(data)

    def test_recent_state_observation_can_confirm_unchanged_score(self):
        data = self.live_request()
        states = [data['event_state'], *(row['event_state'] for row in data['references'].values())]
        for state in states:
            state['confirmed_at'] = '2026-11-18T11:47:00Z'
        self.assertEqual(Decimal('2.06'), price_from_verified_references(data)['minimum_odds'])

    def test_closed_offer_and_already_determined_totals_are_rejected(self):
        data = self.live_request()
        with self.assertRaisesRegex(ValueError, 'offered quote is not active'):
            price_from_verified_references(data | {'offer_status': 'suspended'})
        with self.assertRaisesRegex(ValueError, 'stale or unavailable'):
            price_from_verified_references(data | {'offer_quoted_at': '2026-11-18T11:48:29Z'})
        data['contract']['line'] = '1.5'
        for quote in data['references'].values():
            quote['contract']['line'] = '1.50'
        with self.assertRaisesRegex(ValueError, 'already exceeds the line'):
            price_from_verified_references(data)


if __name__ == '__main__':
    unittest.main()
