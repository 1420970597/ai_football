from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from reference_stream_replay import compare_rest, read_segment, stream_quote


class ReferenceStreamReplayTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.archive = Path(folder.name)
        self.start = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
        self.binding = {'market': 'fixture-total', 'rules_sha256': 'rules'}
        self.subscription = self.binding | {'kind': 'subscription_market', 'condition_id': 'condition',
                                            'assets': {'Over': 'yes', 'Under': 'no'}}

    def change(self, seconds, token, bid='0.48', *, source_offset=0, ask='0.51'):
        received = self.start + timedelta(seconds=seconds)
        source = self.start + timedelta(seconds=seconds + source_offset)
        event = {'event_type': 'price_change', 'market': 'condition', 'timestamp': str(int(source.timestamp()*1000)),
                 'price_changes': [{'asset_id': token, 'best_bid': bid, 'best_ask': ask}]}
        return {'kind': 'price_change', 'received_at': received.isoformat(), 'event': event}

    def segment(self, name, rows, cutoffs, *, complete=True):
        path = self.archive / name
        entries = [self.subscription, *rows, *([{'kind': 'observation_end'}] if complete else [])]
        path.write_text(''.join(json.dumps(entry)+'\n' for entry in entries))
        return read_segment(path, cutoffs)

    def test_cutoffs_keep_segments_separate_and_use_only_received_prices(self):
        cutoffs = [self.start + timedelta(seconds=n) for n in (1, 3, 10, 21)]
        first = self.segment('first.jsonl', [self.change(0, 'yes'), self.change(2, 'no'),
                                             self.change(3, 'yes', '0.49'), self.change(4, 'yes', '0.50')], cutoffs,
                             complete=False)
        second = self.segment('second.jsonl', [self.change(20, 'no'), self.change(22, 'yes')], cutoffs)
        observations = [stream_quote(self.binding, [first, second], cutoff, 30) for cutoff in cutoffs]
        self.assertEqual(['missing_stream_side', 'observed_top_of_book', 'outside_stream_segment', 'missing_stream_side'],
                         [row['status'] for row in observations])
        self.assertEqual('0.49', observations[1]['books'][0]['bid'])
        self.assertNotIn('bid_size', observations[1]['books'][0])
        self.assertFalse(first[2]['complete'])
        self.assertTrue(second[2]['complete'])
        self.assertEqual('different_rules', stream_quote(self.binding | {'rules_sha256': 'updated'},
                                                        [first], cutoffs[1], 30)['status'])

    def test_source_time_and_rest_comparison(self):
        cutoff = self.start + timedelta(seconds=40)
        rows = [self.change(38, 'no'), self.change(39, 'yes'), self.change(39.5, 'yes', '0.49', source_offset=-31),
                self.change(41, 'yes')]
        segment = self.segment('stream.jsonl', rows, [cutoff])
        observed = stream_quote(self.binding, [segment], cutoff, 30)
        self.assertEqual('0.48', observed['books'][0]['bid'])
        compare_rest(observed, {'status': 'timed_depth', 'books': observed['books']})
        self.assertEqual('same_prices', observed['rest_comparison'])
        books = [observed['books'][0] | {'bid': '0.47'}, observed['books'][1]]
        compare_rest(observed, {'status': 'timed_depth', 'books': books})
        self.assertEqual('different_prices', observed['rest_comparison'])
        self.assertEqual('stale_stream_quote', stream_quote(self.binding, [segment], cutoff, 1)['status'])
        self.assertEqual('ambiguous_stream_segments', stream_quote(self.binding, [segment, segment], cutoff, 30)['status'])
        with self.assertRaisesRegex(ValueError, 'future'):
            self.segment('future.jsonl', [self.change(38, 'yes', source_offset=1)], [cutoff])
