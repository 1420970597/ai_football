from copy import deepcopy
from decimal import Decimal
import unittest

from market_reference import price_from_verified_references
from price_contract import price_contract


def order_book(sides, limits):
    lower, upper = map(Decimal, limits)
    return {
        side: {'bid': bid, 'ask': ask, 'bid_size': '10', 'ask_size': '10'}
        for side, bid, ask in ((sides[0], lower, upper), (sides[1], 1 - upper, 1 - lower))
    }


def request(line='2.75', selection='over', *, lines=('2.5', '3.5'), exact=False, **contract_fields):
    contract = {'fixture_id': 'fixture-1', 'sport': 'football', 'phase': 'scheduled', 'kind': 'total',
                'period': 'full_time', 'score_basis': 'full_period', 'line': line, **contract_fields}
    sides = ('home', 'away') if contract['kind'] == 'handicap' else ('over', 'under')
    quotes = [
        {'contract': dict(contract, line=half), 'order_book': order_book(sides, limits),
         'status': 'active', 'quoted_at': '2026-11-18T11:48:30Z', 'received_at': '2026-11-18T11:48:32Z'}
        for half, limits in zip(lines, (('0.67' if exact else '0.63', '0.67'), ('0.47' if exact else '0.43', '0.47')))
    ]
    return {
        'reference_mode': 'adjacent_half_lines', 'contract': dict(contract, selection=selection),
        'as_of': '2026-11-18T11:48:35Z', 'max_age_seconds': 30, 'max_quote_skew_seconds': 2,
        'offer_venue': 'venue-offer', 'offer_feed_origin': 'feed-offer', 'offer_status': 'active',
        'offer_quoted_at': '2026-11-18T11:48:30Z', 'offer_received_at': '2026-11-18T11:48:32Z',
        'offered_odds': '2', 'cost_per_unit': '0.01', 'required_net_edge': '0.02',
        'references': {source: {'venue': 'venue-' + source, 'feed_origin': 'feed-' + source,
                                'adjacent_quotes': deepcopy(quotes)} for source in ('a', 'b')},
    }


class AdjacentReferenceTests(unittest.TestCase):
    def test_integer_and_quarters_price_adverse_compatible_endpoints(self):
        for line, side, expected_win, expected_loss, active_floor in (
            ('3', 'over', '0.43', '0.37', '0.76'),
            ('2.75', 'over', '0.53', '0.37', '0.88'),
            ('3.25', 'over', '0.43', '0.47', '0.88'),
            ('2.75', 'under', '0.33', '0.57', '0.88'),
            ('3.25', 'under', '0.43', '0.47', '0.88'),
        ):
            with self.subTest(line=line, side=side):
                result = price_from_verified_references(request(line, side))
                scenario = result['scenarios']['a']
                win, loss = map(Decimal, (expected_win, expected_loss))
                self.assertEqual(scenario['expected_win_fraction'], win)
                self.assertEqual(scenario['expected_loss_fraction'], loss)
                self.assertEqual(result['active_stake_lower_bound'], Decimal(active_floor))
                self.assertEqual(result['minimum_odds'], 1 + (loss + Decimal('0.03')) / win)
                self.assertEqual(result['worst_net_at_offered_odds'], win - loss - Decimal('0.01'))

    def test_matches_native_score_settlement_for_all_split_directions(self):
        for line, lines, scores in (
            ('-1.25', ('-0.5', '-1.5'), [(0, 0), (1, 0), (2, 0)]),
            ('-1.00', ('-0.5', '-1.5'), [(0, 0), (1, 0), (2, 0)]),
            ('-0.75', ('-0.5', '-1.5'), [(0, 0), (1, 0), (2, 0)]),
            ('0.25', ('0.5', '-0.5'), [(0, 1), (0, 0), (1, 0)]),
        ):
            for selection in ('home', 'away'):
                with self.subTest(line=line, selection=selection):
                    data = request(line, selection, lines=lines, exact=True,
                                   kind='handicap', line_basis='provider_home')
                    distribution = [{'home': home, 'away': away, 'probability': probability}
                                    for (home, away), probability in zip(scores, ('0.33', '0.20', '0.47'))]
                    native = price_contract(dict(data, scenarios={'native': distribution}))
                    result = price_from_verified_references(data)
                    self.assertEqual(result['worst_net_at_offered_odds'], native['worst_net_at_offered_odds'])
                    self.assertEqual(result['minimum_odds'], native['minimum_odds'])

    def test_verified_basketball_integer_and_football_team_totals(self):
        basketball = request('209', lines=('208.5', '209.5'), sport='basketball',
                             settlement_scope='including_overtime')
        result = price_from_verified_references(basketball)
        self.assertEqual(result['worst_net_at_offered_odds'], Decimal('0.05'))
        team = request(kind='team_total', team_side='away')
        self.assertEqual(price_from_verified_references(team)['worst_net_at_offered_odds'], Decimal('0.15'))
        team['references']['a']['adjacent_quotes'][0]['contract']['team_side'] = 'home'
        with self.assertRaisesRegex(ValueError, 'reference contract differs'):
            price_from_verified_references(team)

    def test_incompatible_books_and_line_order_do_not_price(self):
        for change, reason in (
            ('duplicate_line', 'required lines'), ('no_depth', 'positive depth'),
            ('no_overlap', 'inconsistent probability ranges'), ('wrong_order', 'inconsistent scoring'),
            ('old_timestamp', 'allowed skew'),
        ):
            with self.subTest(change=change):
                data = request()
                looser, tighter = data['references']['a']['adjacent_quotes']
                if change == 'duplicate_line':
                    tighter['contract']['line'] = looser['contract']['line']
                elif change == 'no_depth':
                    tighter['order_book']['over']['bid_size'] = '0'
                elif change == 'no_overlap':
                    tighter['order_book']['under'].update(bid='0.48', ask='0.50')
                elif change == 'wrong_order':
                    looser['order_book'], tighter['order_book'] = tighter['order_book'], looser['order_book']
                else:
                    tighter['quoted_at'] = '2026-11-18T11:48:26Z'
                with self.assertRaisesRegex(ValueError, reason):
                    price_from_verified_references(data)

    def test_overlapping_intervals_are_constrained_by_monotonicity(self):
        data = request('3')
        looser, tighter = data['references']['a']['adjacent_quotes']
        looser['order_book'] = order_book(('over', 'under'), ('0.40', '0.70'))
        tighter['order_book'] = order_book(('over', 'under'), ('0.50', '0.80'))
        result = price_from_verified_references(data)['scenarios']['a']
        self.assertEqual(result['expected_win_fraction'], Decimal('0.50'))
        self.assertEqual(result['expected_loss_fraction'], Decimal('0.50'))

    def test_each_live_line_must_match_the_same_game_state(self):
        data = request(phase='live')
        state = {'period': 'second_half', 'status': 'playing', 'home_score': 1, 'away_score': 1,
                 'home_red_cards': 0, 'away_red_cards': 0, 'confirmed_at': '2026-11-18T11:48:28Z',
                 'received_at': '2026-11-18T11:48:29Z'}
        data['event_state'] = state
        for reference in data['references'].values():
            for quote in reference['adjacent_quotes']:
                quote['event_state'] = dict(state)
        self.assertTrue(price_from_verified_references(data)['passes_price_test'])
        beyond = deepcopy(data)
        beyond['contract']['line'] = '3.25'
        beyond['event_state']['home_score'] = 2
        for reference in beyond['references'].values():
            for sample in reference['adjacent_quotes']:
                sample['event_state']['home_score'] = 2
        with self.assertRaisesRegex(ValueError, 'exceeds an adjacent reference line'):
            price_from_verified_references(beyond)
        quote = data['references']['b']['adjacent_quotes'][1]
        quote['event_state']['home_red_cards'] = 1
        with self.assertRaisesRegex(ValueError, 'reference live state differs'):
            price_from_verified_references(data)
        quote['event_state'] = dict(state, confirmed_at='2026-11-18T11:48:31Z', received_at='2026-11-18T11:48:32Z')
        with self.assertRaisesRegex(ValueError, 'quote predates'):
            price_from_verified_references(data)
