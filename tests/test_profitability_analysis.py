from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import unittest

from infra.profitability.analysis_report import reference_evidence, render, settlement_evidence
from infra.profitability.collect_analysis import QUERIES, prepare_query


class ProfitabilityAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 19, 13, tzinfo=timezone.utc)

    def test_prepares_existing_sql_using_text_parameters(self):
        folder = Path(__file__).resolve().parents[1] / 'infra/profitability'
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

    def test_reference_uses_latest_past_replay_and_tracks_failed_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [{'references': [{'status': 'timed_depth', 'outcome_status': 'mapped'}],
                     'score_comparison': {'status': 'observed_score_matches'}}]
            (root / 'fixture-replay-2026-11-19T12:00:00Z.json').write_text('{}')
            (root / 'fixture-replay-2026-11-19T12:59:00Z.json').write_text(json.dumps({
                'capture_rounds': 200, 'preflights': rows,
            }))
            (root / 'fixture-replay-2026-11-19T13:01:00Z.json').write_text('{}')
            (root / 'fixture-smoke-replay-2026-11-19T12:55:00Z.json').write_text('bad')
            matched, smoke = reference_evidence(root, self.now)
            self.assertEqual(matched['archived_at'].minute, 59)
            self.assertEqual(matched['with_mapped_timed_quote_and_matching_score'], 1)
            self.assertTrue(smoke['smoke'])
            self.assertEqual(smoke['status'], 'JSONDecodeError')

    def test_report_preserves_partial_failures(self):
        report = render({'observed_at': self.now.isoformat(), 'settlement': {'status': 'stale_or_after_cutoff'},
                         'references': [], 'diagnostics': {'source_holdout': {'error': 'QueryCanceledError'}}})
        self.assertIn('QueryCanceledError', report)
        self.assertIn('stale_or_after_cutoff', report)


if __name__ == '__main__':
    unittest.main()
