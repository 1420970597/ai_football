from datetime import datetime, timezone
import unittest

from reference_orderbook_stream import book_event


class PublicBookEventTests(unittest.TestCase):
    def setUp(self):
        self.received = datetime(2026, 11, 19, 0, 0, 0, 120_000, tzinfo=timezone.utc)
        self.book = {'event_type': 'book', 'market': 'contract', 'asset_id': 'yes',
                     'timestamp': str(int(self.received.timestamp() * 1000) - 40)}
        self.conditions = {'yes': 'contract', 'no': 'contract', 'other': 'other-contract'}

    def test_initial_snapshot_keeps_native_timestamp_separate_from_receive_time(self):
        initialized = set()
        snapshot = book_event(self.book, self.received, self.conditions, initialized)
        self.assertEqual('initial_book', snapshot['kind'])
        self.assertEqual(self.book['timestamp'], snapshot['event']['timestamp'])
        self.assertEqual(self.received.isoformat(), snapshot['received_at'])
        update = book_event(self.book, self.received, self.conditions, initialized)
        self.assertEqual('book_update', update['kind'])
        self.assertEqual({'yes'}, initialized)

    def test_incremental_events_require_native_time_and_exact_assets(self):
        change = {'event_type': 'price_change', 'market': 'contract', 'timestamp': self.book['timestamp'],
                  'price_changes': [{'asset_id': 'yes'}, {'asset_id': 'no'}]}
        initialized = set()
        self.assertEqual('price_change', book_event(change, self.received, self.conditions, initialized)['kind'])
        self.assertFalse(initialized)
        for invalid in ({'timestamp': 'none'}, {'timestamp': str(int(self.received.timestamp() * 1000) + 1)},
                        {'price_changes': [{'asset_id': 'other'}]}, {'price_changes': [{'asset_id': 'unknown'}]},
                        {'price_changes': []}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                book_event(change | invalid, self.received, self.conditions, initialized)
