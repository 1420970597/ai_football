import json
from pathlib import Path
import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta
from contextlib import nullcontext
from unittest.mock import patch

from core.learned_model import DATA_VERSION, FEATURES
from core.live_model import MODEL_VERSION
from store.ledger_db import LedgerDB
from store.training_data import (
    _dataset_postgres,
    _postgres_training_sample_query,
    dataset,
    snapshot_inputs,
    write_inputs,
)
from store.training_projection import ProjectionTarget


def decision():
    common = {'p_market': .5, 'p_model': .6, 'ts_ms': 1000, 'quote_received_at_ms': 1000, 'research_only': False,
              'decision_status': 'settleable', 'settlement_basis': 'full_score', 'training_history': [[100, 2], [900, 2.1], [2000, 9]], 'trend_pct': 350}
    candidates = [{**common, 'market': 'HAD', 'line': '', 'outcome': 'home'},
                  {**common, 'market': 'OU', 'line': '2/2.5', 'outcome': 'over'},
                  {**common, 'settlement_basis': 'remaining_score', 'market': 'AH', 'line': '-0.5', 'outcome': 'away'},
                  {**common, 'settlement_basis': 'half_score_required', 'market': 'HAD_1H', 'line': '', 'outcome': 'home'}]
    return {'model_version': MODEL_VERSION, 'competition_type': 'real', 'match_id': 'm1', 'computed_at': '2025-01-01T12:00:00+00:00',
            'score': [0,0], 'elapsed_s': 1200, 'input_cutoff_ms': 1000, 'received_at_ms': 1000,
            'evaluations': [{'algorithm': a, 'candidates': candidates} for a in ('poisson_market', 'poisson_time_decay')]}


class TrainingDataTests(unittest.TestCase):

    def test_postgres_insert_batches_are_bounded_and_parameterized(self):
        from store.training_data import _insert_postgres_rows
        calls = []
        class Cursor:
            def execute(self,sql,values):
                calls.append((sql,values))
        rows = [(str(i),"a'b") for i in range(513)]
        _insert_postgres_rows(Cursor(),'model_predictions',('version','match_id'),rows,
                              '(version,match_id) DO NOTHING')
        self.assertEqual(len(calls),3)
        self.assertEqual([len(values) for _,values in calls],[512,512,2])
        self.assertFalse(any("a'b" in sql for sql,_ in calls))
        self.assertEqual(calls[0][1][:2],('0',"a'b"))
        with self.assertRaises(ValueError):
            _insert_postgres_rows(Cursor(),'unsafe',('version',),[], '(version) DO NOTHING')

    def test_prospective_persist_uses_same_pg_session_and_preserves_prediction(self):
        from store.training_data import persist_prospective_predictions
        from tests.test_shadow_observation import ShadowObservationTests
        row = ShadowObservationTests().source_row()
        connection = object()
        class Cursor:
            def close(self):
                pass
        class Connection:
            def cursor(self):
                return Cursor()
        connection = Connection()
        with patch('store.training_data.prepare_postgres_schema'), \
                patch('store.training_data.persistent_writer',return_value=nullcontext(connection)) as writer, \
                patch('store.training_data._shadow_label_subset',return_value={}) as labels, \
                patch('store.training_data._insert_postgres_rows') as insert:
            actual = persist_prospective_predictions(Path('ledger'),Path('shadow'),[row],
                target=ProjectionTarget.parse('postgresql://fixture/model'))
        self.assertEqual(actual,[row])
        self.assertEqual(writer.call_count,1)
        self.assertIs(labels.call_args.args[0],connection)
        self.assertEqual(insert.call_args.args[3][0][10],row['probability'])
        self.assertEqual(insert.call_args.args[3][0][11],row['predicted_at'])

    def postgres_audit(self, source_rows, estimate=(23_000_000, -.0001), *, max_per_match=40):
        statements = []
        columns = list(source_rows[0]) if source_rows else []

        class Cursor:
            description = [(name,) for name in columns]

            def execute(self, operation, parameters=()):
                statements.append(operation)

            def fetchone(self):
                return estimate

            def fetchmany(self, size):
                batch, source_rows[:] = source_rows[:size], source_rows[size:]
                return [tuple(row[name] for name in columns) for row in batch]

            def close(self):
                pass

        class Connection:
            def cursor(self):
                return Cursor()

        finals = {'m1': {'ft_score': (2, 1), 'ht_score': (1, 0),
                         'settled_at': '2025-01-01T14:00:00+00:00'}}
        quality = {}
        with patch('store.training_data.readonly_snapshot', return_value=nullcontext(Connection())), \
                patch('store.training_data._labels_postgres', return_value=finals):
            rows = _dataset_postgres(ProjectionTarget.parse('postgresql://localhost/model'),
                                     audit=quality, max_per_match=max_per_match)
        return rows, quality, statements

    def test_postgres_quality_does_not_certify_unaudited_archive_totals(self):
        source = snapshot_inputs(decision())[:2]
        source[1]['settlement_basis'] = 'unknown'
        rows, quality, statements = self.postgres_audit(source)
        self.assertEqual(len(rows), 1)
        self.assertTrue(quality['sampled'])
        self.assertEqual(quality['counts_scope'], 'sampled_qualified_inputs')
        self.assertFalse(quality['full_audit_complete'])
        self.assertEqual(quality['sample_examined_records'], 2)
        self.assertEqual(quality['sample_examined_matches'], 1)
        self.assertEqual(quality['sample_valid_training_decisions'], 1)
        self.assertEqual(quality['sample_ungraded_inputs'], 1)
        self.assertEqual(quality['estimated_input_records'], 23_000_000)
        self.assertEqual(quality['estimated_input_matches'], 2300)
        for field in ('input_records', 'input_matches', 'valid_training_decisions', 'waiting_for_label',
                      'quarantined_inputs', 'quarantined_ledger', 'quarantined_missing_time_contract',
                      'eligible_time_contract', 'labelled_time_contract', 'invalid_training_records',
                      'source_eligible_decisions'):
            with self.subTest(field=field):
                self.assertIsNone(quality[field])
        # Sampling is bounded independently from exact archive quality counts.
        self.assertFalse(any('SELECT COUNT(' in operation.upper() for operation in statements))
        self.assertEqual(quality['sampling_method'], 'time_stratified_market_curves_per_match')
        self.assertEqual(quality['sample_label_matches'], 1)
        self.assertEqual(quality['sample_max_rows_per_match'], 2)

    def test_postgres_empty_sample_leaves_unknown_quality_unknown(self):
        for estimate in (None, (-1, None), (0, None)):
            with self.subTest(estimate=estimate):
                rows, quality, _ = self.postgres_audit([], estimate)
                self.assertEqual(rows, [])
                self.assertEqual(quality['sample_examined_records'], 0)
                self.assertEqual(quality['sample_valid_training_decisions'], 0)
                self.assertIsNone(quality['valid_training_decisions'])
                self.assertIsNone(quality['invalid_training_records'])
                self.assertIsNone(quality['waiting_for_label'])
                self.assertIsNone(quality['estimated_input_matches'])
                self.assertEqual(quality['estimated_input_records'], 0 if estimate == (0, None) else None)

    def test_postgres_uncapped_quality_only_certifies_qualified_inputs(self):
        source = snapshot_inputs(decision())[:1]
        rows, quality, _ = self.postgres_audit(source, max_per_match=None)
        self.assertEqual(len(rows), 1)
        self.assertFalse(quality['sampled'])
        self.assertEqual(quality['counts_scope'], 'qualified_inputs')
        self.assertFalse(quality['full_audit_complete'])
        self.assertEqual(quality['source_eligible_decisions'], 1)
        self.assertEqual(quality['valid_training_decisions'], 1)
        self.assertIsNone(quality['quarantined_inputs'])
        self.assertIsNone(quality['waiting_for_label'])

    def test_postgres_training_query_uses_bounded_stratified_time_sampling(self):
        labels = "(SELECT match_id, available_at FROM latest_labels)"
        time_filter = "cutoff_ms > 0 AND decision_received_at_ms <= cutoff_ms AND quote_received_at_ms <= cutoff_ms"
        query, args = _postgres_training_sample_query(
            labels, time_filter, ("data-v", "model-v", "feature-v"), 40)

        self.assertIn("CROSS JOIN LATERAL", query)
        self.assertIn("generate_series(0,7)", query)
        self.assertIn("GROUP BY i.algorithm,i.market", query)
        self.assertIn("BOOL_AND(COALESCE(", query)
        self.assertIn("ORDER BY i.at ASC", query)
        self.assertIn("ORDER BY i.at DESC", query)
        self.assertIn("time_bucket.lower_at", query)
        self.assertIn("i.at<=time_bucket.upper_at", query)
        self.assertIn("selected_point.at", query)
        self.assertIn("hashtextextended(concat_ws", query)
        self.assertNotIn("COUNT(*) OVER", query)
        self.assertEqual(args, ("data-v", "model-v", "feature-v") * 4)
        self.assertIn("l.available_at > i.at", query)
        self.assertIn("i.decision_received_at_ms <= i.cutoff_ms", query)
        self.assertIn("i.quote_received_at_ms <= i.cutoff_ms", query)

    def test_evenly_spaced_sampling_returns_exact_cap_for_shared_divisors(self):
        for total, cap in ((100, 40), (120, 40), (12, 4), (41, 40), (4, 40)):
            selected = [row for row in range(1, total + 1)
                        if row * cap // total > (row - 1) * cap // total]
            with self.subTest(total=total, cap=cap):
                self.assertEqual(len(selected), min(total, cap))
                if total > cap:
                    self.assertEqual(selected[-1], total)

    def test_sqlite_sampling_keeps_full_cap_across_match_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = LedgerDB(root/'ledger.sqlite3')
            final = {'match_id':'m1', 'at':'2025-01-01T12:00:00+00:00',
                     'settled_at':'2025-01-01T14:00:00+00:00', 'competition_type':'real',
                     'status':'won', 'ft_score':[2,1], 'ht_score':[1,0],
                     'label_available_at':'2025-01-01T14:00:00+00:00'}
            db.put([final], lambda _:1.0)
            db.connection.commit()
            base = datetime.fromisoformat('2025-01-01T12:00:00+00:00')
            rows = []
            for second in range(100):
                source = decision()
                source['computed_at'] = (base + timedelta(seconds=second)).isoformat()
                source['input_cutoff_ms'] = 1000 + second
                source['received_at_ms'] = 1000 + second
                rows.extend(snapshot_inputs(source))
            path = root/'inputs.sqlite3'
            write_inputs(path, rows)

            sampled = dataset(db.path, path, max_per_match=40)

            sampled_times = sorted({row['at'] for row in sampled})
            self.assertEqual(len(sampled), 40)
            self.assertEqual(len(sampled_times), 40)
            self.assertGreater(sampled_times[0], base.isoformat())
            self.assertEqual(sampled_times[-1], (base + timedelta(seconds=99)).isoformat())

    def test_grouped_sampling_preserves_complete_had_across_time_and_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = LedgerDB(root/'ledger.sqlite3')
            final = {'match_id':'m1', 'at':'2025-01-01T12:00:00+00:00',
                     'settled_at':'2025-01-01T14:00:00+00:00', 'competition_type':'real',
                     'status':'won', 'ft_score':[2,1], 'ht_score':[1,0],
                     'label_available_at':'2025-01-01T14:00:00+00:00'}
            db.put([final], lambda _:1.0)
            db.connection.commit()
            rows = []
            base = datetime.fromisoformat(final['at'])
            for second in range(100):
                source = decision()
                source['computed_at'] = (base + timedelta(seconds=second)).isoformat()
                common = source['evaluations'][0]['candidates'][0]
                source['evaluations'] = [{'algorithm':'poisson_market',
                    'candidates':[{**common, 'outcome':o} for o in ('home','draw','away')]}]
                rows.extend(snapshot_inputs(source))
            path = root/'inputs.sqlite3'
            write_inputs(path, rows)
            sampled = dataset(db.path, path, max_per_match=40)
            groups = {}
            for row in sampled:
                groups.setdefault(row['at'], set()).add(row['outcome'])
            self.assertTrue(groups)
            self.assertTrue(all(outcomes == {'home','draw','away'} for outcomes in groups.values()))
            self.assertLessEqual(len(sampled), 120)
            self.assertEqual(max(groups), (base + timedelta(seconds=99)).isoformat())
            final.update(ft_score=[1,1], ht_score=[0,0])
            db.put([final], lambda _:2.0)
            db.connection.commit()
            changed = dataset(db.path, path, max_per_match=40)
            def identity(values):
                return [(r['at'],r['algorithm'],r['market'],r['line'],r['outcome']) for r in values]
            self.assertEqual(identity(sampled), identity(changed))
            self.assertNotEqual([r['y'] for r in sampled], [r['y'] for r in changed])

    def test_postgres_capped_reader_does_not_split_group_a_second_time(self):
        source = snapshot_inputs(decision())[:1]
        common = source[0]
        source = [{**common, 'outcome':o} for o in ('home','draw','away')]
        rows, quality, _ = self.postgres_audit(source, max_per_match=1)
        self.assertEqual({r['outcome'] for r in rows}, {'home','draw','away'})
        self.assertIsNone(quality['sampling_row_bound'])
        self.assertEqual(quality['sampling_unit'], 'match_id,at,algorithm,market')

    def test_market_curve_sampling_preserves_cross_line_coherence(self):
        from core.model_validation import coherent_probabilities
        from store.training_data import _selected_contract_rows
        source = []
        for second in range(10):
            for line, probabilities in (('2.5', (.4, .6)), ('3.5', (.7, .3))):
                for outcome, probability in zip(('over', 'under'), probabilities):
                    source.append({'match_id':'m1', 'at':str(second), 'algorithm':'a',
                                   'market':'OU', 'line':line, 'outcome':outcome,
                                   'probability':probability})
        sampled = [row for row, selected in _selected_contract_rows(source, {'m1':40}, 1) if selected]
        self.assertEqual(len(sampled), 4)
        self.assertEqual({row['line'] for row in sampled}, {'2.5', '3.5'})
        full_curve = source[-4:]
        self.assertEqual(coherent_probabilities(sampled, [r['probability'] for r in sampled]),
                         coherent_probabilities(full_curve, [r['probability'] for r in full_curve]))
        for actual, expected in zip(coherent_probabilities(sampled, [r['probability'] for r in sampled]),
                                    [.55, .45, .55, .45]):
            self.assertAlmostEqual(actual, expected)

    def test_sqlite_invalid_clock_quarantines_whole_market_curve(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = LedgerDB(root/'ledger.sqlite3')
            final = {'match_id':'m1', 'at':'2025-01-01T12:00:00+00:00',
                     'settled_at':'2025-01-01T14:00:00+00:00', 'competition_type':'real',
                     'status':'won', 'ft_score':[2,1], 'ht_score':[1,0],
                     'label_available_at':'2025-01-01T14:00:00+00:00'}
            db.put([final], lambda _:1.0)
            db.connection.commit()
            common = snapshot_inputs(decision())[1]
            rows = [{**common, 'line':line, 'outcome':outcome}
                    for line in ('2.5','3.5') for outcome in ('over','under')]
            rows[-1]['quote_received_at_ms'] = 1001
            path = root/'inputs.sqlite3'
            write_inputs(path, rows)
            quality = {}
            self.assertEqual(dataset(db.path, path, audit=quality, max_per_match=1), [])
            self.assertEqual(quality['quarantined_missing_time_contract'], 1)
            self.assertEqual(quality['quarantined_incomplete_market_curve_inputs'], 3)

    def test_postgres_training_query_without_sampling_keeps_time_boundary(self):
        labels = "(SELECT match_id, available_at FROM latest_labels)"
        query, args = _postgres_training_sample_query(
            labels, "cutoff_ms > 0", ("d", "m", "f"), None)

        self.assertNotIn("CROSS JOIN LATERAL", query)
        self.assertIn("SELECT i.* FROM model_inputs i JOIN", query)
        self.assertIn("l.available_at > i.at", query)
        self.assertEqual(args, ("d", "m", "f"))

    def test_label_revisions_are_append_only_and_latest_available_score_is_selected(self):
        from store.training_data import labels
        with tempfile.TemporaryDirectory() as directory:
            db = LedgerDB(Path(directory) / 'ledger.sqlite3')
            first = {'match_id': 'm1', 'decision_id': 'd1', 'at': '2025-01-01T12:00:00+00:00',
                     'settled_at': '2025-01-01T14:00:00+00:00',
                     'label_available_at': '2025-01-01T14:00:00+00:00', 'updated_at': '2025-01-01T14:00:00+00:00',
                     'competition_type': 'real', 'status': 'won', 'ft_score': [1, 0], 'ht_score': [1, 0]}
            db.put([first], lambda _: 1.0)
            db.connection.commit()
            revised = {**first, 'settled_at': '2025-01-01T15:00:00+00:00',
                       'label_available_at': '2025-01-01T15:00:00+00:00',
                       'updated_at': '2025-01-01T15:00:00+00:00', 'ft_score': [1, 1], 'ht_score': [0, 0]}
            db.put([revised], lambda _: 2.0)
            db.connection.commit()
            revisions = db.connection.execute('SELECT count(*) FROM label_revisions').fetchone()[0]
            self.assertEqual(revisions, 2)
            self.assertEqual(labels(db.connection)['m1']['ft_score'], (1, 1))

    def test_training_sample_is_bounded_reproducible_and_result_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = LedgerDB(root/'ledger.sqlite3')
            final = {'match_id':'m1', 'at':'2025-01-01T12:00:00+00:00',
                     'settled_at':'2025-01-01T14:00:00+00:00', 'competition_type':'real',
                     'status':'won', 'ft_score':[2,1], 'ht_score':[1,0], 'label_available_at':'2025-01-01T14:00:00+00:00'}
            db.put([final], lambda _:1.0)
            db.connection.commit()
            path = root/'inputs.sqlite3'
            write_inputs(path, snapshot_inputs(decision()))
            quality = {}
            a = dataset(db.path,path,max_per_match=3,audit=quality)
            b = dataset(db.path,path,max_per_match=3)
            self.assertEqual(a,b)
            self.assertEqual(len(a),3)
            self.assertEqual(quality['source_eligible_decisions'],8)
            self.assertEqual(quality['input_records'],8)
            self.assertEqual(quality['valid_training_decisions'],8)
            self.assertEqual(quality['invalid_training_records'],0)
            self.assertEqual(quality['counts_scope'], 'full_inputs')
            self.assertTrue(quality['full_audit_complete'])
            self.assertFalse(quality['sampled'])
            final['ft_score'] = [0,3]
            final['ht_score'] = [0,1]
            db.put([final],lambda _:2.0)
            db.connection.commit()
            c = dataset(db.path,path,max_per_match=3)
            self.assertEqual([r['x'] for r in a],[r['x'] for r in c])
            with self.assertRaises(ValueError):
                dataset(db.path,path,max_per_match=0)

    def test_dataset_counts_input_matches_and_prefilters_labels_by_availability_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = LedgerDB(root / 'ledger.sqlite3')
            final = {'match_id': 'm1', 'at': '2025-01-01T10:00:00+00:00',
                     'settled_at': '2025-01-01T11:00:00+00:00',
                     'label_available_at': '2025-01-01T11:00:00+00:00',
                     'competition_type': 'real', 'status': 'won',
                     'ft_score': [2, 1], 'ht_score': [1, 0]}
            db.put([final], lambda _: 1.0)
            db.connection.commit()
            path = root / 'inputs.sqlite3'
            write_inputs(path, snapshot_inputs(decision()))

            quality = {}
            rows = dataset(db.path, path, audit=quality)

            self.assertEqual(rows, [])
            self.assertEqual(quality['input_matches'], 1)
            self.assertEqual(quality['eligible_time_contract'], 8)
            self.assertEqual(quality['waiting_for_label'], 8)
            self.assertEqual(quality['valid_training_decisions'], 0)
            self.assertGreaterEqual(quality['quarantined_missing_time_contract'], 0)
            self.assertLessEqual(quality['eligible_time_contract'], quality['input_records'])

    def test_old_features_are_quarantined_without_relabeling_as_clean(self):
        old = decision()
        old['model_version'] = 'live-poisson-market-v1'
        self.assertEqual(snapshot_inputs(old), [])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = LedgerDB(root/'ledger.sqlite3')
            path = root/'inputs.sqlite3'
            rows = snapshot_inputs(decision())
            write_inputs(path, rows)
            with sqlite3.connect(path) as con:
                con.execute('UPDATE inputs SET data_version=NULL,entry_score=NULL')
            quality = {}
            self.assertEqual(dataset(db.path,path,audit=quality), [])
            self.assertEqual(quality['quarantined_inputs'],8)
            self.assertEqual(quality['valid_training_decisions'],0)
            self.assertEqual(quality['invalid_training_records'],8)

    def test_remaining_labels_require_benchmark_and_weight_half_stake(self):
        from store.training_data import target
        final = {'ft_score':[1,0],'ht_score':[1,0]}
        row = {'market':'AH','outcome':'home','line':'-0/0.5',
               'entry_score':'[1,0]','settlement_basis':'remaining_score'}
        self.assertEqual(target(row,final),(0,.5))
        self.assertEqual(target({**row,'market':'AH_1H'},final),(0,.5))
        self.assertIsNone(target({**row,'settlement_basis':'full_score'},final))
        self.assertIsNone(target({**row,'entry_score':None},final))
        self.assertIsNone(target({**row,'entry_score':'[2,0]'},final))
        self.assertEqual(snapshot_inputs(decision())[0]['data_version'],DATA_VERSION)

    def test_ou_and_ah_labels_follow_asian_line_payment_weights(self):
        from store.training_data import target
        final = {'ft_score': [1, 0], 'ht_score': [1, 0]}
        base = {'entry_score': [0, 0]}
        cases = (
            ({**base, 'market': 'OU', 'outcome': 'over', 'line': '2/2.5',
              'settlement_basis': 'full_score'}, (0, 1.0)),
            ({**base, 'market': 'OU', 'outcome': 'under', 'line': '1/1.5',
              'settlement_basis': 'full_score'}, (1, .5)),
            ({**base, 'market': 'AH', 'outcome': 'home', 'line': '-0/0.5',
              'settlement_basis': 'remaining_score'}, (1, 1.0)),
            ({**base, 'entry_score': [1, 0], 'market': 'AH', 'outcome': 'away', 'line': '0/0.5',
              'settlement_basis': 'remaining_score'}, (0, .5)),
        )
        for row, expected in cases:
            with self.subTest(row=row):
                self.assertEqual(target(row, final), expected)

    def test_all_markets_algorithms_and_strict_prefix(self):
        rows = snapshot_inputs(decision())
        self.assertEqual(len(rows), 8)
        for row in rows:
            x = json.loads(row['x'])
            self.assertAlmostEqual(x[FEATURES.index('trend_return')], .05)
            self.assertEqual(x[FEATURES.index('trend_points')], 2/40)
        source = decision()
        source['evaluations'][0]['candidates'][0]['ts_ms'] = 1001
        self.assertEqual(len(snapshot_inputs(source)), 6)  # candidate list shared by two test algorithms
        source.pop('input_cutoff_ms')
        self.assertEqual(snapshot_inputs(source), [])

    def test_late_received_history_is_excluded_from_offline_features(self):
        source = decision()
        # Event time is before cutoff, but the quote was first received after it.
        source['evaluations'][0]['candidates'][0]['training_history'] = [
            [100, 2.0, 900], [950, 2.2, 950], [990, 3.0, 1100]]
        source['evaluations'][0]['candidates'][0]['quote_received_at_ms'] = 900
        rows = snapshot_inputs(source)
        row = next(r for r in rows if r['algorithm'] == 'poisson_market' and r['market'] == 'HAD')
        x = json.loads(row['x'])
        self.assertAlmostEqual(x[FEATURES.index('trend_return')], 0.1)
        self.assertEqual(x[FEATURES.index('trend_points')], 2 / 40)

    def test_missing_or_late_dual_time_evidence_is_quarantined(self):
        source = decision()
        source.pop('received_at_ms')
        self.assertEqual(snapshot_inputs(source), [])
        source = decision()
        source['evaluations'][0]['candidates'][0]['quote_received_at_ms'] = 1001
        rows = snapshot_inputs(source)
        self.assertEqual(len(rows), 6)

    def test_ledger_fallback_cannot_bypass_dual_time_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = LedgerDB(root / 'ledger.sqlite3')
            final = {'match_id':'m1', 'at':'2025-01-01T12:00:00+00:00',
                     'settled_at':'2025-01-01T14:00:00+00:00', 'competition_type':'real',
                     'status':'won', 'ft_score':[2,1], 'ht_score':[1,0],
                     'label_available_at':'2025-01-01T14:00:00+00:00'}
            source = decision()
            payload = {'match_id':'m1', 'at':source['computed_at'], 'algorithm':'poisson_market',
                       'market':'HAD', 'line':'', 'outcome':'home', 'status':'won',
                       'competition_type':'real', 'trigger':'live_forecast', 'research_only':0,
                       'model_version':MODEL_VERSION, 'p_market':.5, 'settled_at':final['settled_at']}
            db.put([final, payload], lambda _:1.0)
            db.connection.commit()
            # No certified input snapshot exists. A plausible ledger payload and
            # matching label must remain quarantined instead of rebuilding features.
            with sqlite3.connect(root / 'inputs.sqlite3') as inputs:
                inputs.execute('CREATE TABLE inputs (match_id TEXT, at TEXT, data_version TEXT, '
                               'model_version TEXT, feature_version TEXT)')
            quality = {}
            self.assertEqual(dataset(db.path, root / 'inputs.sqlite3', audit=quality), [])
            self.assertEqual(quality['quarantined_ledger'], 1)

    def test_same_snapshot_peer_probabilities_are_combined(self):
        source = decision()
        source['evaluations'][1]['candidates'] = [{**c,'p_model':.8} for c in source['evaluations'][1]['candidates']]
        rows=snapshot_inputs(source)
        x=json.loads(rows[0]['x'])
        self.assertGreater(x[FEATURES.index('peer_poisson_time_decay')],x[FEATURES.index('peer_poisson_market')])
        self.assertAlmostEqual(x[FEATURES.index('peer_missing')],.6)

    def test_new_samples_settle_each_market_half_scores_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = LedgerDB(root / 'ledger.sqlite3')
            final = {'match_id': 'm1', 'at': '2025-01-01T12:00:00+00:00', 'settled_at': '2025-01-01T14:00:00+00:00',
                     'competition_type': 'real', 'status': 'won', 'ft_score': [2,1], 'ht_score': [1,0],
                     'label_available_at': '2025-01-01T14:00:00+00:00'}
            db.put([final], lambda value: datetime.fromisoformat(value).timestamp())
            db.connection.commit()
            path = root / 'inputs.sqlite3'
            write_inputs(path, snapshot_inputs(decision()))
            rows = dataset(db.path, path)
            self.assertEqual(len(rows), 8)
            self.assertEqual(len({r['match_id'] for r in rows}), 1)
            self.assertEqual({r['y'] for r in rows}, {0,1})
            write_inputs(path, snapshot_inputs(decision()))
            self.assertEqual(len(dataset(db.path, path)), 8)
            final.pop('ht_score')
            final['label_available_at'] = '2025-01-01T14:30:00+00:00'
            db.put([final], lambda value: datetime.fromisoformat(value).timestamp())
            db.connection.commit()
            self.assertEqual(len(dataset(db.path, path)), 6)
            final['settled_at'] = '2025-01-01T11:00:00+00:00'
            final['label_available_at'] = '2025-01-01T11:00:00+00:00'
            db.put([final], lambda value: datetime.fromisoformat(value).timestamp())
            db.connection.commit()
            # Append-only labels retain the latest actually available revision;
            # a later-arriving backdated correction cannot erase it.
            self.assertEqual(len(dataset(db.path, path)), 6)
