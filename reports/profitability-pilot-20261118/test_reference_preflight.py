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
        self.book = {'status': 'two_sided', 'quote_time': '2026-11-19T12:02:50Z',
                     'received_at': '2026-11-19T12:02:52Z'}
        self.market = {'source': 'polymarket', 'market': 'match-half-1pt5', 'rules_sha256': 'rules',
                       'accepting_orders': True, 'books': [self.book, self.book.copy()]}

    def compare(self, *markets):
        # A later market in the same round may complete after this preflight.
        batch = {'captured_at': '2026-11-19T12:03:03Z', 'markets': markets}
        row, = compare([self.preflight], [self.binding], [batch], 30)
        return row['references']

    def test_market_must_be_available_before_preflight_with_native_times(self):
        self.assertEqual('timed_depth', self.compare(self.market)[0]['status'])
        self.assertEqual(10, self.compare(self.market)[0]['oldest_quote_age_seconds'])
        self.market['books'][1]['received_at'] = '2026-11-19T12:03:01Z'
        self.assertEqual('no_observation_before_preflight', self.compare(self.market)[0]['status'])

    def test_preflight_never_reverts_to_previous_open_book(self):
        shut = deepcopy(self.market)
        shut['accepting_orders'] = False
        shut['books'][0]['received_at'] = '2026-11-19T12:02:55Z'
        self.assertEqual('market_not_open', self.compare(shut, self.market)[0]['status'])

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
