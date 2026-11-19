import unittest

from reference_basketball_state import game_state


class BasketballReferenceStateTests(unittest.TestCase):
    def setUp(self):
        self.game = {
            'id': '101', 'date': '2026-11-19T02:30Z', 'status': {'type': {'state': 'pre'}},
            'competitors': [
                {'homeAway': 'away', 'team': {'abbreviation': 'BOS'}},
                {'homeAway': 'home', 'team': {'abbreviation': 'DEN'}},
            ],
        }
        self.data = {'header': {'id': '101', 'competitions': [self.game]}}

    def test_preserves_missing_pregame_score(self):
        result = game_state(self.data, '101', 'DEN', 'BOS')
        self.assertEqual({'home': ('DEN', None), 'away': ('BOS', None)}, result['teams'])
        self.assertEqual({'type': {'state': 'pre'}}, result['status'])
        self.assertEqual([], result['plays'])

    def test_refuses_swapped_teams_or_mismatched_event(self):
        with self.assertRaises(ValueError):
            game_state(self.data, '101', 'BOS', 'DEN')
        self.game['id'] = '102'
        with self.assertRaises(ValueError):
            game_state(self.data, '101', 'DEN', 'BOS')

    def test_preserves_tied_or_out_of_order_plays(self):
        plays = [
            {'id': 'first', 'wallclock': '2026-11-19T02:37:05Z'},
            {'id': 'second', 'wallclock': '2026-11-19T02:37:04Z'},
            {'id': 'third', 'wallclock': '2026-11-19T02:37:04Z', 'homeScore': 2, 'awayScore': 0},
        ]
        result = game_state(self.data | {'plays': plays}, '101', 'DEN', 'BOS')
        self.assertEqual(plays, [{k: row[k] for k in value} for row, value in zip(result['plays'], plays)])
