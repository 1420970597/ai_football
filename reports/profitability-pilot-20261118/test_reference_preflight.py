from copy import deepcopy
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
