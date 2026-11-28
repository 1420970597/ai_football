from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import Mock, patch

from reference_orderbook_stream import book_event, capture


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

    def test_resumed_capture_stops_at_original_deadline(self):
        socket = Mock()
        socket.recv.side_effect = [json.dumps([self.book, self.book | {'asset_id': 'no'}]),
                                   json.dumps(self.book)]
        connector = Mock()
        connector.__enter__ = Mock(return_value=socket)
        connector.__exit__ = Mock(return_value=False)
        within = self.received + timedelta(seconds=1)
        after = self.received + timedelta(seconds=6)
        with patch('reference_orderbook_stream.polymarket_metadata',
                   return_value=({'condition_id': 'contract', 'rules_sha256': 'rules'}, [('Yes', 'yes'), ('No', 'no')])), \
             patch('reference_orderbook_stream.connect', return_value=connector), \
             patch('reference_orderbook_stream.datetime') as wall_time, \
             patch('reference_orderbook_stream.monotonic', side_effect=[100, 100, 100, 101]):
            wall_time.now.side_effect = [self.received, within, after]
            rows = list(capture(['market'], 900, self.received.timestamp() + 5))
        self.assertEqual(['subscription_market', 'initial_book', 'initial_book', 'observation_end'],
                         [row['kind'] for row in rows])
        self.assertEqual(5, rows[-1]['observation_seconds'])
