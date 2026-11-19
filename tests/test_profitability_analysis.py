from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import tempfile
import unittest
from urllib.parse import urlsplit

import asyncpg

from infra.profitability.analysis_report import reference_evidence, render, settlement_evidence
from infra.profitability.collect_analysis import QUERIES, prepare_query

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE = os.environ.get('TEST_DATABASE_ADMIN_URL')


class ProfitabilityAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 19, 13, tzinfo=timezone.utc)

    def test_prepares_existing_sql_using_text_parameters(self):
        folder = ROOT / 'infra/profitability'
        params = dict.fromkeys(('begin', 'end', 'anchor', 'split', 'analysis_end', 'receipt_as_of', 'observed'), self.now)
        for name in QUERIES:
            with self.subTest(name=name):
                sql, values = prepare_query((folder / f'{name}.sql').read_text(), params)
                self.assertNotIn('ROLLBACK;', sql)
                self.assertNotIn(":'begin'", sql)
                self.assertEqual(values, [self.now.isoformat()] * len(values))
        query, _ = prepare_query((folder / 'snapshot_fill_comparison.sql').read_text(), params)
        self.assertIn("nullif($2::text,'')::timestamptz", query)

    def test_stale_settlement_snapshot_does_not_contribute_amounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'state').mkdir()
            (root / 'state/latest.json').write_text(json.dumps({
                'observed_at': (self.now - timedelta(minutes=12)).isoformat(), 'windows': [{'provider_net': '88'}],
            }))
            report = settlement_evidence(root, self.now)
            self.assertEqual(report['status'], 'stale_or_after_cutoff')
            self.assertNotIn('windows', report)

    def test_settlement_uses_previous_checkpoint_when_latest_is_after_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoints = root / 'state/checkpoints'
            checkpoints.mkdir(parents=True)
            snapshot = {
                'anchor': (self.now - timedelta(hours=3)).isoformat(),
                'observed_at': (self.now + timedelta(microseconds=1)).isoformat(),
                'windows': [], 'unknown_total': 0, 'pending_total': 2,
            }
            (root / 'state/latest.json').write_text(json.dumps(snapshot))
            (checkpoints / '20261119T130000Z.json').write_text(json.dumps(snapshot))
            prior = snapshot | {'observed_at': (self.now - timedelta(minutes=4)).isoformat(), 'pending_total': 1}
            (checkpoints / '20261119T125600Z.json').write_text(json.dumps(prior))
            (checkpoints / '20261119T125800Z.json').write_text('{')
            (checkpoints / '20261119T124800Z.json').write_text(json.dumps(
                prior | {'observed_at': (self.now - timedelta(minutes=12)).isoformat()}))
            report = settlement_evidence(root, self.now)
            self.assertEqual(report['status'], 'ok')
            self.assertEqual(report['pending_total'], 1)
            self.assertEqual(report['snapshot_file'], 'state/checkpoints/20261119T125600Z.json')
            (checkpoints / '20261119T125600Z.json').unlink()
            self.assertEqual(settlement_evidence(root, self.now)['status'], 'stale_or_after_cutoff')

    def test_reference_uses_latest_past_replay_and_tracks_failed_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [{'preflight': {'sport': 'football', 'phase': 'live', 'period': 'full_time', 'kind': 'total'},
                     'references': [{'status': 'timed_depth', 'outcome_status': 'mapped'},
                                    {'status': 'stale_before_preflight', 'stream': {'status': 'observed_top_of_book'}}],
                     'score_comparison': {'status': 'observed_score_matches'},
                     'settlement_comparison': 'unknown_settlement_basis',
                     'source_groups': {'polymarket': {'status': 'incomplete'}},
                     'adjacent_line_coverage': {'source_groups': {'polymarket': {'status': 'paired'}},
                                                'lines': [{'references': [{'stream': {'status': 'outside_stream_segment'}}]}]}}]
            (root / 'fixture-replay-2026-11-19T12:00:00Z.json').write_text('{}')
            (root / 'fixture-replay-2026-11-19T12:59:00Z.json').write_text(json.dumps({
                'capture_rounds': 200, 'preflights': rows,
            }))
            (root / 'fixture-replay-2026-11-19T13:01:00Z.json').write_text('{}')
            (root / 'fixture-smoke-replay-2026-11-19T12:55:00Z.json').write_text('bad')
            matched, smoke = reference_evidence(root, self.now)
            self.assertEqual(matched['archived_at'].minute, 59)
            self.assertEqual(matched['with_mapped_timed_quote_and_matching_score'], 1)
            self.assertEqual(matched['with_stream_top_of_book'], 1)
            self.assertEqual(matched['with_complete_quote_group'], 0)
            self.assertEqual(matched['with_paired_adjacent_lines'], 1)
            self.assertEqual(matched['adjacent_stream_quote_statuses'], {'outside_stream_segment': 1})
            self.assertEqual(matched['settlement_comparison_statuses'], {'unknown_settlement_basis': 1})
            self.assertEqual(matched['by_contract'][0]['kind'], 'total')
            self.assertTrue(smoke['smoke'])
            self.assertEqual(smoke['status'], 'JSONDecodeError')

    def test_report_preserves_partial_failures(self):
        report = render({'observed_at': self.now.isoformat(), 'settlement': {'status': 'stale_or_after_cutoff'},
                         'references': [], 'diagnostics': {'source_holdout': {'error': 'QueryCanceledError'}}})
        self.assertIn('QueryCanceledError', report)
        self.assertIn('stale_or_after_cutoff', report)

    def test_report_distinguishes_order_overlap_from_evidence_gaps(self):
        overlap = {'native_fills': 15, 'fixtures': 4, 'same_contract': 2, 'nearby_open_contract': 1,
                   'missing_placement': 1, 'missing_contract': 1, 'reused_receipt': 1, 'uncertain_ordering': 2}
        report = render({'observed_at': self.now.isoformat(), 'settlement': {'status': 'stale_or_after_cutoff'},
                         'references': [], 'diagnostics': {'order_overlap': {'data': overlap}}})
        self.assertIn('2 笔相同合约及 1 笔前单未结的自动相邻线', report)
        self.assertIn('不能确定前后顺序 2 笔', report)


@unittest.skipUnless(TEST_DATABASE, 'isolated Docker PostgreSQL required')
class ProfitabilityOverlapDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def test_counts_contract_overlap_without_ambiguous_or_reused_receipts(self):
        parts = urlsplit(TEST_DATABASE)
        if (parts.scheme, parts.username, parts.hostname, parts.path) != (
                'postgresql', 'postgres', 'ai-football-v2-pgverify', '/postgres'):
            raise RuntimeError('Overlap test requires the isolated Docker test database')
        begin = datetime(2026, 11, 18, 2, tzinfo=timezone.utc)
        query, args = prepare_query((ROOT / 'infra/profitability/order_overlap.sql').read_text(), {
            'begin': begin, 'end': begin + timedelta(hours=1),
        })
        conn = await asyncpg.connect(TEST_DATABASE, timeout=5, command_timeout=7)
        try:
            await conn.fetchval("SELECT set_config('profitability_test.starts_at', $1, false)", begin.isoformat())
            fixture = ROOT / 'tests/integration/profitability_order_overlap.sql'
            await conn.execute(fixture.read_text())
            async with conn.transaction(isolation='repeatable_read', readonly=True):
                result = json.loads(await conn.fetchval(query, *args))
        finally:
            await conn.close()
        self.assertEqual({
            'fixtures': 4, 'native_fills': 16, 'automatic_fills': 15, 'same_contract': 3,
            'nearby_open_contract': 1, 'reused_receipt': 1, 'uncertain_ordering': 2,
            'missing_contract': 1, 'missing_placement': 1,
        }, result)


if __name__ == '__main__':
    unittest.main()
