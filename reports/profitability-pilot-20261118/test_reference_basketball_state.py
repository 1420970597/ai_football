import unittest
from datetime import datetime

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

    def test_soccer_requires_exact_league_and_kickoff_and_retains_native_event_time(self):
        self.data['header']['league'] = {'slug': 'bra.1'}
        self.data['keyEvents'] = [
            {'id': 'goal-1', 'type': {'type': 'goal'}, 'wallclock': '2026-11-19T02:37:05Z'},
            {'id': 'card-1', 'type': {'type': 'red-card'}},
        ]
        kickoff = datetime.fromisoformat(self.game['date'])
        result = game_state(self.data, '101', 'DEN', 'BOS', league='bra.1', kickoff=kickoff)
        self.assertEqual(['goal-1', 'card-1'], [event['id'] for event in result['key_events']])
        self.assertEqual('2026-11-19T02:37:05Z', result['key_events'][0]['wallclock'])
        self.assertIsNone(result['key_events'][1]['wallclock'])
        self.assertNotIn('wallclock', result['status'])
        for league, start in [('bra.2', kickoff), ('bra.1', kickoff.replace(minute=31))]:
            with self.subTest(league=league, start=start), self.assertRaises(ValueError):
                game_state(self.data, '101', 'DEN', 'BOS', league=league, kickoff=start)

    def test_soccer_stat_counts_are_bound_to_team_ids_and_reported_when_explicit(self):
        self.data['header']['league'] = {'slug': 'bra.1'}
        self.game['competitors'][0]['team']['id'] = 'away-101'
        self.game['competitors'][1]['team']['id'] = 'home-101'
        self.data['keyEvents'] = [{'id': 'dismissal', 'team': {'id': 'away-101'}}]
        home = {'team': {'id': 'home-101'}, 'statistics': [{'name': 'redCards', 'displayValue': '0'}]}
        away = {'team': {'id': 'away-101'}, 'statistics': [{'name': 'redCards', 'displayValue': '1'}]}
        self.data['boxscore'] = {'teams': [away, home]}
        kickoff = datetime.fromisoformat(self.game['date'])
        result = game_state(self.data, '101', 'DEN', 'BOS', league='bra.1', kickoff=kickoff)
        self.assertEqual({'home': 0, 'away': 1}, result['red_cards'])
        self.assertEqual('away-101', result['key_events'][0]['team_id'])
        self.assertTrue(result['key_events_available'])
        self.data['boxscore']['teams'] = [away, away]
        del self.data['keyEvents']
        result = game_state(self.data, '101', 'DEN', 'BOS', league='bra.1', kickoff=kickoff)
        self.assertEqual({'home': None, 'away': None}, result['red_cards'])
        self.assertFalse(result['key_events_available'])
