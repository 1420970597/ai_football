from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from infra.profitability.experiments.event_coverage import analyze


START = datetime.fromisoformat('2026-11-20T05:00:00Z')
END = START + timedelta(minutes=2)


class EventCoverageTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.frames = self.root / 'frames'
        self.frames.mkdir()
        self.costs = self.root / 'costs'
        self.costs.mkdir()
        self.created = START - timedelta(seconds=55)
        self.row = {'signal_id': 'private-signal', 'fixture_id': 'private-fixture', 'sport': 'football',
                    'phase': 'scheduled', 'market_kind': 'total', 'selection': 'over',
                    'signal_at': self.created, 'signal_price': '1.87',
                    'source_at': self.created - timedelta(seconds=3),
                    'recommendation_book': {
                        'market_instance_id': 'm', 'play_id': 'p', 'received_at': self.created,
                        'options': [{'selection_id': 'o', 'selection': 'over', 'price': '1.87'},
                                    {'selection_id': 'u', 'selection': 'under', 'price': '1.98'}]},
                    'preflights': [{'checked_at': self.created + timedelta(seconds=9),
                                    'captured_at': self.created + timedelta(seconds=10),
                                    'native_book': {'Over': '1.88', 'Under': '1.97'}, 'price': '1.88',
                                    'native_accepted': True, 'reference_contract_missing': True,
                                    'price_decision': 'reference_ineligible'}]}
        self.data = {'schema': 1, 'begin': START - timedelta(seconds=90), 'end': END,
                     'cutoff': END, 'rows': [self.row]}
        self.save_frame()

    def save_frame(self, signal_at=None):
        path = self.frames / '2026-11-20T05:00:00Z.json'
        frame = {'slot': START, 'begin': START - timedelta(seconds=90), 'end': START - timedelta(seconds=30),
                 'observed_at': START + timedelta(seconds=5), 'rows': [
                     {'signal_id': self.row['signal_id'], 'created_at': signal_at or self.created,
                      'status': 'current_book_missing_or_incomplete'}]}
        path.write_text(json.dumps(frame, default=str))

    def evaluate(self, fixtures=None):
        return analyze(self.data, self.frames, {'fixtures': fixtures or {}}, self.costs, START, END)

    def test_matches_identity_and_clock_without_exposing_private_records(self):
        result = self.evaluate()
        self.assertEqual(result['slots'], {'valid': 1, 'missing': 1})
        self.assertEqual(result['coverage']['paired_signals'], 1)
        self.assertEqual(result['coverage']['snapshot_fresh'], 1)
        self.assertEqual(result['coverage']['complete_preflight_book_before_minute'], 1)
        self.assertEqual(result['attempts']['reference_contract_missing'], 1)
        self.assertNotIn('private-', json.dumps(result, default=str))

    def test_rejects_same_identity_with_different_event_time(self):
        self.save_frame(self.created + timedelta(seconds=1))
        result = self.evaluate()
        self.assertEqual(result['coverage'], {'event_without_same_minute_signal': 1})

    def test_future_snapshot_and_late_preflight_are_excluded(self):
        self.row['recommendation_book']['received_at'] = START
        self.row['preflights'][0]['captured_at'] = END + timedelta(seconds=1)
        result = self.evaluate()
        self.assertEqual(result['coverage']['snapshot_time_invalid'], 1)
        self.assertEqual(result['coverage']['without_verified_preflight_clock'], 1)
        self.assertEqual(result['attempts'], {})

    def test_duplicate_event_cannot_inflate_coverage(self):
        self.data['rows'].append(self.row.copy())
        with self.assertRaisesRegex(ValueError, 'duplicate event identity'):
            self.evaluate()

    def test_preflight_checked_before_minute_but_saved_later_is_not_visible(self):
        attempt = self.row['preflights'][0]
        attempt['checked_at'] = START + timedelta(seconds=4)
        attempt['captured_at'] = START + timedelta(seconds=6)
        result = self.evaluate()
        self.assertEqual(result['coverage']['with_verified_preflight_clock'], 1)
        self.assertNotIn('preflight_before_minute_observation', result['coverage'])
        self.assertNotIn('complete_preflight_book_before_minute', result['coverage'])

    def test_cost_digest_is_evidence_availability_only(self):
        content = b'official source test artifact'
        digest = sha256(content).hexdigest()
        (self.costs / digest).write_bytes(content)
        (self.costs / ('a' * 64)).write_bytes(b'bad content')
        fixture = {'approvals': [{'cost_schedule': {'evidence_sha256': digest}}]}
        result = self.evaluate({'private-fixture': fixture})['reference_data']
        self.assertEqual(result['verified_digests'], 1)
        self.assertEqual(result['invalid_digests'], 1)
        self.assertEqual(result['schedules_with_matching_digest'], 1)
        self.assertFalse(result['account_cost_certified_by_this_report'])
