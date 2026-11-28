from datetime import datetime
from copy import deepcopy
import json
import unittest

from expand_reference_catalog import bind_catalog


class CatalogExpansionTests(unittest.TestCase):
    def setUp(self):
        self.begin = '2026-11-19T17:45:00Z'
        self.label = 'verified-event'
        self.catalog = {
            'slug': self.label + '-more-markets', 'startTime': self.begin,
            'teams': [{'ordering': 'home', 'abbreviation': 'hm', 'name': 'Home FC'},
                      {'ordering': 'away', 'abbreviation': 'aw', 'name': 'Away FC'}],
            'markets': [self.market('spread-away-1pt5', 'spreads', -1.5, ['Away FC', 'Home FC']),
                        self.market('first-half-team-total-home-0pt5', 'soccer_first_half_team_totals', 0.5,
                                    ['Over', 'Under'], 'Fixture: Home FC 1st Half O/U 0.5'),
                        self.market('total-2pt5', 'totals', 2.5, ['Over', 'Under'])],
        }

    def market(self, suffix, kind, line, labels, question='Fixture'):
        return {'slug': f'{self.label}-{suffix}', 'sportsMarketType': kind, 'line': line,
                'description': 'Fixture-specific verified terms', 'outcomes': json.dumps(labels),
                'clobTokenIds': '["token-1","token-2"]', 'conditionId': suffix, 'acceptingOrders': True,
                'gameStartTime': self.begin, 'question': question}

    def bind(self, catalog):
        return bind_catalog(catalog, self.label, 'leyu:100', 'hm', 'aw', datetime.fromisoformat(self.begin),
                            {f'{self.label}-total-2pt5'})

    def test_market_directions_and_existing_markets(self):
        expanded = self.bind(self.catalog)
        self.assertEqual(datetime.fromisoformat(self.begin), datetime.fromisoformat(expanded['kickoff_at']))
        handicap, team = expanded['bindings']
        self.assertEqual(('handicap', '1.5'), (handicap['kind'], handicap['line']))
        self.assertEqual(('team_total', 'first_half', 'home'), (team['kind'], team['period'], team['team_side']))
        self.assertEqual({'Home FC': 'home', 'Away FC': 'away'},
                         expanded['outcomes']['polymarket'][handicap['market']]['labels'])
        self.assertNotIn(f'{self.label}-total-2pt5', expanded['outcomes']['polymarket'])

    def test_refuses_wrong_team_kickoff_or_sign(self):
        for problem in ('team', 'kickoff', 'sign', 'label', 'duplicate'):
            data = deepcopy(self.catalog)
            if problem == 'team':
                data['teams'][0]['abbreviation'] = 'aw'
            elif problem == 'kickoff':
                data['markets'][0]['gameStartTime'] = '2026-11-19T20:00:00Z'
            elif problem == 'sign':
                data['markets'][0]['line'] = 1.5
            elif problem == 'label':
                data['markets'][1]['question'] = 'Fixture: Away FC 1st Half O/U 0.5'
            else:
                data['markets'].append(data['markets'][1])
            with self.subTest(problem=problem), self.assertRaises(ValueError):
                self.bind(data)
