from copy import deepcopy
from decimal import Decimal
import unittest

from market_reference import price_from_verified_references
from price_contract import price_contract
from test_adjacent_reference import order_book


def request(*, winner=False):
    source = {'fixture_id': 'fixture-1', 'sport': 'football', 'phase': 'scheduled',
              'kind': 'winner', 'period': 'full_time', 'score_basis': 'full_period',
              'settlement_scope': 'regulation', 'line': None}
    target = dict(source, selection='away')
    if not winner:
        target.update(kind='handicap', line='0.00', line_basis='provider_home')
    outcomes = {}
    for outcome, bounds in (('home', ('0.40', '0.41')), ('draw', ('0.26', '0.27')),
                            ('away', ('0.33', '0.34'))):
        books = order_book(('yes', 'no'), bounds)
        for book in books.values():
            book.update(quoted_at='2026-11-18T11:48:30Z', received_at='2026-11-18T11:48:32Z')
        outcomes[outcome] = {'contract': dict(source, selection=outcome), 'status': 'active', 'order_book': books}
    return {
        'reference_mode': 'three_way_books', 'contract': target,
        'as_of': '2026-11-18T11:48:35Z', 'max_age_seconds': 30, 'max_quote_skew_seconds': 2,
        'offer_venue': 'leyu', 'offer_feed_origin': 'leyu', 'offer_status': 'active',
        'offer_quoted_at': '2026-11-18T11:48:30Z', 'offer_received_at': '2026-11-18T11:48:32Z',
        'offered_odds': '2', 'cost_per_unit': '0.01', 'required_net_edge': '0.02',
        'references': {source: {'venue': 'venue-' + source, 'feed_origin': 'feed-' + source,
                                'outcome_quotes': deepcopy(outcomes)} for source in ('a', 'b')},
    }


class ThreeWayBookTests(unittest.TestCase):
    def test_draw_no_bet_matches_native_settlement_at_worst_feasible_distribution(self):
        data = request()
        native = price_contract(dict(data, scenarios={'same': [
            {'home': 0, 'away': 1, 'probability': '0.33'},
            {'home': 2, 'away': 2, 'probability': '0.26'},
            {'home': 2, 'away': 1, 'probability': '0.41'},
        ]}))
        priced = price_from_verified_references(data)
        self.assertEqual(native['minimum_odds'], priced['minimum_odds'])
        self.assertEqual(Decimal('-0.09'), priced['worst_net_at_offered_odds'])
        self.assertEqual(Decimal('0.73'), priced['active_stake_lower_bound'])

    def test_winner_uses_worst_feasible_probability_and_keeps_draw_as_a_loss(self):
        data = request(winner=True)
        priced = price_from_verified_references(data)
        self.assertEqual(Decimal('0.33'), priced['scenarios']['a']['expected_win_fraction'])
        self.assertEqual(Decimal('0.67'), priced['scenarios']['a']['expected_loss_fraction'])
        self.assertEqual(Decimal('-0.35'), priced['worst_net_at_offered_odds'])
        self.assertEqual(Decimal(1), priced['active_stake_lower_bound'])

    def test_rejects_inconsistent_probabilities_and_missing_or_mismatched_evidence(self):
        data = request()
        with self.assertRaisesRegex(ValueError, 'two independent'):
            price_from_verified_references(dict(data, references={'a': data['references']['a']}))
        invalid = deepcopy(data)
        invalid['contract']['line'] = '0.25'
        with self.assertRaisesRegex(ValueError, 'draw-no-bet'):
            price_from_verified_references(invalid)
        for change, error in (
            ('missing_outcome', 'home, draw and away'), ('stale_side', 'allowed skew'),
            ('different_selection', 'reference outcome'), ('inconsistent_ranges', 'complete outcome distribution'),
        ):
            with self.subTest(change=change):
                invalid = deepcopy(data)
                cases = invalid['references']['a']['outcome_quotes']
                if change == 'missing_outcome':
                    cases.pop('draw')
                elif change == 'stale_side':
                    cases['draw']['order_book']['no']['quoted_at'] = '2026-11-18T11:48:26Z'
                elif change == 'different_selection':
                    cases['draw']['contract']['selection'] = 'away'
                else:
                    for side, book in cases['draw']['order_book'].items():
                        book.update(order_book(('yes', 'no'), ('0.45', '0.5'))[side])
                with self.assertRaisesRegex(ValueError, error):
                    price_from_verified_references(invalid)

    def test_live_draw_no_bet_requires_the_same_verified_score_and_cards(self):
        data = request()
        data['contract']['phase'] = 'live'
        state = {'period': 'first_half', 'status': 'playing', 'home_score': 0, 'away_score': 0,
                 'home_red_cards': 0, 'away_red_cards': 0, 'confirmed_at': '2026-11-18T11:48:29Z',
                 'received_at': '2026-11-18T11:48:30Z'}
        data['event_state'] = state
        for reference in data['references'].values():
            for sample in reference['outcome_quotes'].values():
                sample['contract']['phase'] = 'live'
                sample['event_state'] = dict(state)
        price_from_verified_references(data)
        data['references']['b']['outcome_quotes']['draw']['event_state']['home_score'] = 1
        with self.assertRaisesRegex(ValueError, 'reference live state differs'):
            price_from_verified_references(data)
