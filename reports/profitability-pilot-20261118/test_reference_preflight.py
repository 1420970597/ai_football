from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from reference_preflight import compare


class ReferencePreflightTests(unittest.TestCase):
    def setUp(self):
        self.preflight = {'fixture_id': 'leyu:123', 'kind': 'total', 'period': 'first_half',
                          'line': '1.500', 'captured_at': '2026-11-19T12:03:00Z'}
        self.binding = self.preflight | {'source': 'polymarket', 'market': 'match-half-1pt5',
                                         'line': '1.5', 'rules_sha256': 'rules'}
        self.book = {'outcome': 'Over', 'status': 'two_sided', 'quote_time': '2026-11-19T12:02:50Z',
                     'bid': '0.60', 'ask': '0.61', 'bid_size': '1.250', 'ask_size': '2.125',
                     'received_at': '2026-11-19T12:02:52Z'}
        self.market = {'source': 'polymarket', 'market': 'match-half-1pt5', 'rules_sha256': 'rules',
                       'accepting_orders': True, 'books': [self.book, self.book | {'outcome': 'Under'}]}

    def compare(self, *markets):
        # A later market in the same round may complete after this preflight.
        batch = {'captured_at': '2026-11-19T12:03:03Z', 'markets': markets}
        row, = compare([self.preflight], [self.binding], [batch], 30)
        return row['references']

    def test_market_must_be_available_before_preflight_with_native_times(self):
        match, = self.compare(self.market)
        self.assertEqual('timed_depth', match['status'])
        self.assertEqual(10, match['oldest_quote_age_seconds'])
        self.assertEqual(['Over', 'Under'], [row['outcome'] for row in match['books']])
        self.assertEqual({'outcome': 'Over', 'bid': '0.60', 'ask': '0.61', 'bid_size': '1.250',
                          'ask_size': '2.125', 'quoted_at': self.book['quote_time'],
                          'received_at': self.book['received_at']}, match['books'][0])
        self.assertEqual('rules', match['rules_sha256'])
        self.market['books'][1]['received_at'] = '2026-11-19T12:03:01Z'
        self.assertEqual('no_observation_before_preflight', self.compare(self.market)[0]['status'])

    def test_invalid_depth_evidence_does_not_expose_price(self):
        for broken in ({'ask': '0.59'}, {'bid': 'NaN'}, {'bid_size': '0'}, {'outcome': 'Over'}, {'outcome': None}):
            with self.subTest(broken=broken):
                market = self.market | {'books': [self.book, self.market['books'][1] | broken]}
                match, = self.compare(market)
                self.assertEqual('invalid_depth_evidence', match['status'])
                self.assertNotIn('books', match)
        match, = self.compare(self.market | {'books': [self.book]})
        self.assertEqual('invalid_depth_evidence', match['status'])

    def test_preflight_never_reverts_to_previous_open_book(self):
        shut = deepcopy(self.market)
        shut['accepting_orders'] = False
        shut['books'][0]['received_at'] = '2026-11-19T12:02:55Z'
        self.assertEqual('market_not_open', self.compare(shut, self.market)[0]['status'])

    def test_out_of_order_archives_keep_latest_before_each_cutoff(self):
        first = self.preflight | {'captured_at': '2026-11-19T12:02:54Z'}
        next_order = self.preflight | {'captured_at': '2026-11-19T12:02:59Z'}
        shut = deepcopy(self.market)
        shut['accepting_orders'] = False
        for book in shut['books']:
            book['received_at'] = '2026-11-19T12:02:55Z'
        reopened = shut | {'accepting_orders': True}
        later = {'source': 'polymarket', 'market': self.binding['market'], 'error': 'TimeoutError'}
        archive = iter([
            {'captured_at': '2026-11-19T12:03:01Z', 'markets': [later]},
            {'captured_at': '2026-11-19T12:02:55Z', 'markets': [shut]},
            {'captured_at': '2026-11-19T12:02:52Z', 'markets': [self.market]},
            {'captured_at': '2026-11-19T12:02:55Z', 'markets': [reopened]},
        ])
        rows = compare([next_order, first], [self.binding], archive, 30)
        self.assertEqual(['market_not_open', 'timed_depth'],
                         [row['references'][0]['status'] for row in rows])

    def test_missing_stale_and_inconsistent_source_time_remain_visible(self):
        for quoted, expected in (
            (None, 'missing_native_time'),
            ('2026-11-19T12:02:29.999999Z', 'stale_before_preflight'),
            ('2026-11-19T12:02:53Z', 'invalid_native_time'),
        ):
            with self.subTest(quoted=quoted):
                self.book['quote_time'] = quoted
                self.assertEqual(expected, self.compare(self.market)[0]['status'])

    def test_contract_and_rule_must_match(self):
        self.market['rules_sha256'] = 'new-rules'
        self.assertEqual('different_rules', self.compare(self.market)[0]['status'])
        self.binding['period'] = 'full_time'
        self.assertEqual([], self.compare(self.market))

    def test_exact_outcome_mapping_keeps_market_direction_and_rules(self):
        preflight = self.preflight | {'kind': 'handicap', 'period': 'full_time', 'line': '1.5'}
        binding = self.binding | preflight
        market = self.market | {'books': [self.book | {'outcome': 'Away team'},
                                           self.book | {'outcome': 'Home team'}]}
        orientation = {'rules_sha256': 'rules', 'labels': {'Home team': 'home', 'Away team': 'away'}}

        def replay(mapping):
            batch = {'captured_at': preflight['captured_at'], 'markets': [market]}
            row, = compare([preflight], [binding], [batch], 30,
                           {'polymarket': {binding['market']: mapping}})
            return row['references'][0], row['source_groups']['polymarket']

        reference, group = replay(orientation)
        self.assertEqual(['away', 'home'], [book['canonical_outcome'] for book in reference['books']])
        self.assertEqual('complete', group['status'])
        for mapping, status in ((None, 'mapping_missing'),
                                (orientation | {'rules_sha256': 'changed'}, 'mapping_rules_changed'),
                                (orientation | {'labels': {'Home team': 'home'}}, 'mapping_labels_changed'),
                                (orientation | {'labels': {'Home team': 'away', 'Away team': 'away'}}, 'mapping_invalid')):
            with self.subTest(status=status):
                reference, group = replay(mapping)
                self.assertEqual(status, reference['outcome_status'])
                self.assertNotIn('canonical_outcome', reference['books'][0])
                self.assertEqual('incomplete', group['status'])

    def test_three_way_groups_require_distinct_complete_propositions_within_source(self):
        preflight = self.preflight | {'kind': 'winner', 'period': 'full_time', 'line': None}
        selections = ('home', 'draw', 'away')
        bindings = [self.binding | preflight | {'market': side} for side in selections]
        markets = [self.market | {'market': side, 'books': [self.book | {'outcome': 'Yes'},
                                                           self.book | {'outcome': 'No'}]} for side in selections]
        orientations = {side: {'rules_sha256': 'rules', 'selection': side, 'labels': {'Yes': 'yes', 'No': 'no'}}
                        for side in selections}

        def replay(records):
            batch = {'captured_at': preflight['captured_at'], 'markets': records}
            row, = compare([preflight], bindings, [batch], 30, {'polymarket': orientations})
            return row['source_groups']['polymarket']

        self.assertEqual('complete', replay(markets)['status'])
        self.assertEqual(['away'], replay(markets[:2])['missing'])
        self.assertEqual('incomplete', replay(markets[:2])['status'])
        orientations['away']['selection'] = 'home'
        group = replay(markets)
        self.assertEqual('ambiguous', group['status'])
        self.assertEqual(['home'], group['duplicates'])

    def test_native_us_yes_books_cover_three_propositions_without_entering_price_rule(self):
        preflight = self.preflight | {'kind': 'winner', 'period': 'full_time', 'line': None}
        sides = ('home', 'draw', 'away')
        bindings = [preflight | {'source': 'polymarket_us', 'market': side, 'rules_sha256': 'rules'}
                    for side in sides]
        markets = [binding | {'books': [self.book | {'outcome': 'Yes', 'market_state': 'MARKET_STATE_OPEN'}]}
                   for binding in bindings]
        outcomes = {'polymarket_us': {side: {'selection': side, 'rules_sha256': 'rules', 'labels': {'Yes': 'yes'}}
                                      for side in sides}}
        row, = compare([preflight], bindings, [{'captured_at': preflight['captured_at'], 'markets': markets}],
                       30, outcomes)
        self.assertEqual('complete_yes_books_only', row['source_groups']['polymarket_us']['status'])
        for reference, side in zip(row['references'], sides):
            self.assertEqual(side, reference['proposition'])
            self.assertEqual('mapped_yes_only', reference['outcome_status'])
            self.assertEqual(['yes'], [book['canonical_outcome'] for book in reference['books']])
        markets[0]['books'][0]['quote_time'] = '2026-11-19T12:02:29Z'
        row, = compare([preflight], bindings, [{'captured_at': preflight['captured_at'], 'markets': markets}],
                       30, outcomes)
        self.assertEqual('incomplete', row['source_groups']['polymarket_us']['status'])
        self.assertEqual(['home'], row['source_groups']['polymarket_us']['missing'])

    def test_adjacent_totals_require_two_timely_lines_from_the_same_source(self):
        preflight = self.preflight | {'kind': 'total', 'period': 'full_time', 'line': '2.75'}
        bindings = [self.binding | preflight | {'line': line, 'market': market}
                    for line, market in (('2.5', 'lower'), ('3.5', 'upper'))]
        markets = [self.market | {'market': name} for name in ('lower', 'upper')]
        orientation = {'rules_sha256': 'rules', 'labels': {'Over': 'over', 'Under': 'under'}}
        outcomes = {'polymarket': {b['market']: orientation for b in bindings}}

        def replay(records):
            batch = {'captured_at': preflight['captured_at'], 'markets': records}
            row, = compare([preflight], bindings, [batch], 30, outcomes)
            self.assertFalse(row['references'])
            return row['adjacent_line_coverage']

        covered = replay(markets)
        self.assertEqual(['2.5', '3.5'], [line['line'] for line in covered['lines']])
        self.assertEqual('paired', covered['source_groups']['polymarket']['status'])
        self.assertEqual('incomplete', replay(markets[:1])['source_groups']['polymarket']['status'])
        for upper, expected in (({'quote_time': '2026-11-19T12:02:40Z'}, 'different_quote_times'),
                                ({'received_at': '2026-11-19T12:03:01Z'}, 'incomplete'),
                                ({'status': 'missing_side'}, 'incomplete')):
            changed = markets[1] | {'books': [book | upper for book in self.market['books']]}
            self.assertEqual(expected, replay([markets[0], changed])['source_groups']['polymarket']['status'])
        bindings[1]['source'] = 'polymarket_us'
        self.assertFalse(any(group['status'] == 'paired' for group in replay(markets)['source_groups'].values()))

    def test_timely_book_does_not_prove_the_preflight_settlement_scope(self):
        base = self.preflight | {'sport': 'football', 'kind': 'handicap', 'line_basis': 'provider_home'}
        binding = self.binding | {'kind': 'handicap'}
        batch = {'captured_at': base['captured_at'], 'markets': [self.market]}
        cases = [
            ('scheduled', 'full_period', 'scheduled_rule_review_required'),
            ('live', 'remaining_period', 'remaining_period_reference_missing'),
            ('live', 'full_period', 'independent_live_state_missing'),
            ('live', 'unknown', 'score_basis_unverified'),
        ]
        for phase, basis, expected in cases:
            with self.subTest(phase=phase, basis=basis):
                preflight = base | {'phase': phase, 'effective_score_basis': basis}
                row, = compare([preflight], [binding], [batch], 30)
                self.assertEqual('timed_depth', row['references'][0]['status'])
                self.assertEqual(expected, row['settlement_comparison'])

    def test_score_replay_uses_only_the_latest_eligible_observation(self):
        preflight = self.preflight | {'sport': 'football', 'phase': 'live', 'effective_score_basis': 'full_period',
                                      'score_baseline': {'home': 1, 'away': 0}}
        binding = {'event': '101', 'league': 'bra.1', 'scheduled_start': '2026-11-19T12:00:00Z',
                   'home': 'VAS', 'away': 'INT'}
        observation = {
            'event': '101', 'league': 'bra.1', 'scheduled_start': '2026-11-19T12:00Z',
            'requested_at': '2026-11-19T12:02:48Z', 'received_at': '2026-11-19T12:02:50Z',
            'teams': {'home': ['VAS', '1'], 'away': ['INT', '0']},
            'status': {'period': 1, 'displayClock': "3'", 'type': {'state': 'in'}},
            'key_events': [{'wallclock': '2026-11-19T12:01:10Z'}],
        }

        def replay(*samples):
            row, = compare([preflight], [], [], 30, score_bindings={'leyu:123': binding},
                           score_records=samples)
            self.assertEqual('independent_live_state_missing', row['settlement_comparison'])
            return row['score_comparison']

        future = observation | {'received_at': '2026-11-19T12:03:01Z', 'error': 'TimeoutError'}
        compared = replay(future, observation)
        self.assertEqual('observed_score_matches', compared['status'])
        self.assertEqual([1, 0], compared['score'])
        self.assertEqual(10, compared['age_seconds'])
        self.assertEqual('2026-11-19T12:01:10+00:00', compared['latest_native_event_at'])
        self.assertEqual('no_score_observation_before_preflight', replay(future)['status'])
        cases = [
            ({'received_at': '2026-11-19T12:02:59Z', 'error': 'TimeoutError'}, 'score_source_read_failed'),
            ({'received_at': '2026-11-19T12:02:14Z', 'requested_at': '2026-11-19T12:02:12Z'},
             'stale_score_observation'),
            ({'league': 'bra.2'}, 'score_fixture_mismatch'),
            ({'requested_at': '2026-11-19T12:02:51Z'}, 'invalid_score_observation_time'),
            ({'key_events': [{'wallclock': '2026-11-19T12:02:51Z'}]}, 'invalid_score_event_time'),
            ({'status': {'type': {'state': 'pre'}}}, 'score_phase_mismatch'),
            ({'teams': {'home': ['VAS', '2'], 'away': ['INT', '0']}}, 'score_mismatch'),
            ({'teams': {'home': ['VAS', None], 'away': ['INT', '0']}}, 'reference_score_missing'),
        ]
        for patch, status in cases:
            with self.subTest(status=status):
                self.assertEqual(status, replay(observation | patch)['status'])
        self.assertEqual('score_source_read_failed', replay(observation, observation | cases[0][0])['status'])

    def test_team_total_requires_explicit_matching_team_direction(self):
        offered = self.preflight | {'kind': 'team_total'}
        binding = self.binding | {'kind': 'team_total'}
        batch = {'captured_at': offered['captured_at'], 'markets': [self.market]}
        self.assertEqual([], compare([offered], [binding], [batch], 30)[0]['references'])
        self.assertEqual([], compare([offered | {'team_side': 'away'}],
                                     [binding | {'team_side': 'home'}], [batch], 30)[0]['references'])
        self.assertEqual('timed_depth', compare([offered | {'team_side': 'home'}],
                                                [binding | {'team_side': 'home'}], [batch], 30)[0]['references'][0]['status'])

    def test_live_book_can_predate_a_known_dismissal_or_delay(self):
        preflight = self.preflight | {'sport': 'football', 'phase': 'live', 'effective_score_basis': 'full_period',
                                      'score_baseline': {'home': 1, 'away': 0}}
        score_binding = {'event': '101', 'league': 'bra.1', 'scheduled_start': '2026-11-19T12:00:00Z',
                         'home': 'VAS', 'away': 'INT'}
        card = {'id': 'card', 'type': {'type': 'red-card'}, 'wallclock': '2026-11-19T12:02:54Z', 'team_id': '1002'}
        delay = {'id': 'delay', 'type': {'type': 'start-delay'}, 'wallclock': '2026-11-19T12:02:55Z'}
        sample = score_binding | {
            'requested_at': '2026-11-19T12:02:56Z', 'received_at': '2026-11-19T12:02:58Z',
            'teams': {'home': ['VAS', '1'], 'away': ['INT', '0']},
            'status': {'period': 1, 'type': {'state': 'in'}},
            'team_ids': {'home': '1001', 'away': '1002'}, 'red_cards': {'home': 0, 'away': 1},
            'wallclock_available': True, 'key_events_available': True, 'key_events': [delay, card],
        }
        batch = {'captured_at': preflight['captured_at'], 'markets': [self.market]}

        def replay(observation):
            row, = compare([preflight], [self.binding], [batch], 30, score_records=[observation],
                           score_bindings={'leyu:123': score_binding})
            return row['score_comparison']['event_context'], row['references'][0]

        context, reference = replay(sample)
        self.assertEqual('source_counts_match', context['red_card_status'])
        self.assertEqual('delay_unresolved', context['delay_status'])
        self.assertEqual(['start-delay'], context['latest_material_events'])
        self.assertEqual('quote_predates_known_event', reference['live_event_quote_status'])
        for patch, outcome in (({'key_events': [card, card]}, 'invalid_red_card_event'),
                               ({'red_cards': {'home': 0, 'away': 0}}, 'source_counts_conflict'),
                               ({'key_events_available': False}, 'missing_event_evidence'),
                               ({'red_cards': {}}, 'missing_team_statistics')):
            with self.subTest(patch=patch):
                self.assertEqual(outcome, replay(sample | patch)[0]['red_card_status'])
        ended = delay | {'id': 'ended', 'type': {'type': 'end-delay'}, 'wallclock': '2026-11-19T12:02:57Z'}
        self.assertEqual('delay_ended', replay(sample | {'key_events': [card, ended, delay]})[0]['delay_status'])

    def test_replay_retains_complete_rounds_from_interrupted_capture(self):
        first_round = json.dumps({'markets': [self.market], 'captured_at': self.preflight['captured_at']}).encode() + b'\n'
        shut = deepcopy(self.market)
        shut['accepting_orders'] = False
        shut['books'][0]['received_at'] = '2026-11-19T12:02:59Z'
        last_round = json.dumps({'markets': [shut], 'captured_at': self.preflight['captured_at']}).encode() + b'\n'

        with TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'preflights.json').write_text(json.dumps([self.preflight]))
            (path / 'bindings.json').write_text(json.dumps([self.binding]))
            first = path / 'capture-first.jsonl'
            second = path / 'capture-second.jsonl'
            command = [sys.executable, str(Path(__file__).with_name('reference_preflight.py')),
                       str(path / 'preflights.json'), str(path / 'bindings.json')]
            first.write_bytes(first_round + b'{"markets":["\xe4')
            second.write_bytes(last_round)
            recovered = subprocess.run([*command, first, second], capture_output=True, text=True, timeout=5)
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            report = json.loads(recovered.stdout)
            self.assertEqual((2, 1), (report['capture_rounds'], report['archives_with_incomplete_tail']))
            self.assertEqual('market_not_open', report['preflights'][0]['references'][0]['status'])

            for data in (b'{"markets":', b'{"markets":\n' + first_round):
                with self.subTest(data=data):
                    first.write_bytes(data)
                    result = subprocess.run([*command, first], capture_output=True, text=True, timeout=5)
                    self.assertEqual(1, result.returncode)
                    self.assertEqual('', result.stdout)
