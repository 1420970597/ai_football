"""Training lifecycle regressions without changing test-process resources."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.decision_features import FEATURE_VERSION
from core.learned_model import DATA_VERSION
from core.model_validation import VALIDATION_CONTRACT_VERSION
from service.model_training import (_completed_cohort, atomic_json, backup_active_model,
                                    rollback_active_model, rolling_validation, run)
from tests.test_learned_model import sample_row
from tests.test_model_validation import approved_artifact


class ModelTrainingLifecycleTests(unittest.TestCase):
    @staticmethod
    def progress(rows, *, verified=True):
        mids = sorted({str(row['match_id']) for row in rows})
        return {'match_ids': mids, 'cohort_verified': verified,
                'qualified_labels': {mid: {'settled_at': '2025-01-01T00:00:00+00:00'} for mid in mids},
                'eligible_matches': len(mids), 'eligible_decisions': None,
                'counts_scope': 'qualified_labelled_match_cohort',
                'label_cutoff': '2030-01-01T00:00:00+00:00'}

    @staticmethod
    def approval():
        return approved_artifact()['promotion']

    def test_large_round_dispatches_frozen_cohort_without_local_dataset(self):
        rows = [sample_row(i) for i in range(120)]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, \
             patch('service.model_training.constrain',return_value={}), \
             patch('service.model_training.training_cohort_progress',return_value=self.progress(rows)), \
             patch('service.model_training.estimated_temp_bytes',return_value=101*1024**3), \
             patch('service.model_training.dispatch_remote_training',side_effect=RuntimeError('remote offline')) as remote, \
             patch('service.model_training.dataset') as dataset:
            with self.assertRaisesRegex(RuntimeError,'remote offline'):
                run(Path(directory)/'ledger',100,1,256)
            dataset.assert_not_called()
            self.assertEqual(len(remote.call_args.kwargs['cohort']['match_ids']),120)
            self.assertFalse((Path(directory)/'models'/'active.json').exists())

    def test_estimate_exceeding_free_space_preserves_reserve_and_does_not_load(self):
        rows = [sample_row(i) for i in range(120)]
        pressure = {'pause_optional_writes':False,'free':4*1024**3,
                    'critical_free_bytes':3*1024**3,'reason':''}
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, \
             patch('service.model_training.constrain',return_value={}), \
             patch('service.model_training.disk_pressure',return_value=pressure), \
             patch('service.model_training.training_cohort_progress',return_value=self.progress(rows)), \
             patch('service.model_training.estimated_temp_bytes',return_value=2*1024**3), \
             patch('service.model_training.dataset') as dataset:
            self.assertEqual(run(Path(directory)/'ledger',100,1,256)['state'],'waiting_for_disk')
            dataset.assert_not_called()

    def test_rolling_validation_persists_actual_execution_final_fold(self):
        from core.model_validation import evaluate, rolling_folds
        rows = [{**sample_row(i), 'algorithm': algorithm}
                for i in range(120) for algorithm in ('poisson_time_decay', 'economic_ensemble')]
        folds, evidence, _ = rolling_validation(rows)
        execution = evidence['execution_cohort']
        self.assertTrue(execution['available'])
        self.assertEqual(execution['algorithm'], 'economic_ensemble')
        final_rows = [r for r in rolling_folds(rows)[-1][1] if r['algorithm'] == 'economic_ensemble']
        self.assertEqual(execution['evidence']['matches'], len(final_rows))
        self.assertEqual(execution['model'], folds[-1]['model_calibrated']['by_algorithm']['economic_ensemble'])
        self.assertEqual(execution['baseline'],
                         evaluate(final_rows, [r['baseline'] for r in final_rows])['by_algorithm']['economic_ensemble'])

    def test_training_pipeline_missing_execution_rows_stays_candidate(self):
        rows = [sample_row(i) for i in range(120)]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, \
                patch('service.model_training.constrain', return_value={}), \
                patch('service.model_training.training_cohort_progress', return_value=self.progress(rows)), \
                patch('service.model_training.dataset', return_value=rows):
            root = Path(directory)
            status = run(root / 'ledger', 100, 1, 256)
            self.assertEqual(status['state'], 'candidate')
            self.assertFalse((root / 'models' / 'active.json').exists())
            model = json.loads(next((root / 'models').glob('candidate-*.json')).read_text())
        self.assertFalse(model['promotion']['execution_approved'])
        self.assertFalse(model['promotion_evidence']['execution_cohort']['available'])
        self.assertIn('实际执行算法缺少独立测试证据，不能晋级', model['promotion']['reasons'])

    def test_global_calibration_excludes_labels_unavailable_at_final_test_entry(self):
        rows = [sample_row(i) for i in range(120)]
        rows[98]['settled_at'] = rows[100]['at']
        rows[99]['settled_at'] = rows[101]['at']
        with patch('service.model_training.fit_logistic',
                   return_value={'method': 'identity', 'samples': 38}) as calibrator:
            _, _, calibration = rolling_validation(rows)
        probabilities, outcomes = calibrator.call_args.args
        self.assertEqual(len(probabilities), 38)
        self.assertEqual(outcomes, [row['y'] for row in rows[60:98]])
        self.assertEqual(calibration['excluded_unavailable_labels'], 2)
        self.assertEqual(calibration['label_availability_cutoff'], rows[100]['at'])

    def test_final_validation_scores_exact_saved_weights_and_calibrations(self):
        from core.learned_model import predict, train
        from core.model_validation import evaluate, rolling_folds
        from core.oof_peer import calibrate_peer_vector
        from core.probability_calibration import calibrate_probability
        rows = [sample_row(i) for i in range(120)]
        partition = rolling_folds(rows)[-1]
        model = train(rows, partition=partition, epochs=3)
        folds, _, calibration = rolling_validation(rows, final_model=model)
        final_rows = partition[1]
        probabilities = [calibrate_probability(predict(model['weights'],
            calibrate_peer_vector(row['x'], model['peer_calibration'])), calibration)
            for row in final_rows]
        self.assertEqual(folds[-1]['model_calibrated'], evaluate(final_rows, probabilities))
        self.assertEqual(folds[-1]['training_epochs'], model['training_epochs'])
        self.assertTrue(folds[-1]['evaluates_persisted_parameters'])
        self.assertTrue(all(not fold['evaluates_persisted_parameters'] for fold in folds[:-1]))

    def test_final_validation_rejects_different_model_partition(self):
        from core.learned_model import train
        rows = [sample_row(i) for i in range(120)]
        model = train(rows)
        with self.assertRaisesRegex(ValueError, '分区不一致'):
            rolling_validation(rows, final_model=model)

    def test_persisted_candidate_is_the_independently_scored_final_fold(self):
        from core.model_validation import evaluate
        from core.learned_model import predict
        from core.oof_peer import calibrate_peer_vector
        from core.probability_calibration import calibrate_probability
        rows = [sample_row(i) for i in range(120)]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, \
                patch('service.model_training.constrain', return_value={}), \
                patch('service.model_training.training_cohort_progress', return_value=self.progress(rows)), \
                patch('service.model_training.dataset', return_value=rows):
            root = Path(directory)
            status = run(root / 'ledger', 100, 1, 256)
            model = json.loads(next((root / 'models').glob('candidate-*.json')).read_text())
        self.assertEqual(status['state'], 'candidate')
        test_rows = [row for row in rows if row['match_id'] in model['validation_match_ids']]
        probabilities = [calibrate_probability(predict(model['weights'],
            calibrate_peer_vector(row['x'], model['peer_calibration'])), model['calibration'])
            for row in test_rows]
        self.assertEqual(model['rolling_validation'][-1]['model_calibrated'], evaluate(test_rows, probabilities))
        self.assertEqual(model['validation'], evaluate(test_rows, probabilities))
        self.assertIn('validation_uncalibrated', model)
        self.assertEqual(model['training_epochs'], model['rolling_validation'][-1]['training_epochs'])

    def artifact(self, **changes):
        return {**approved_artifact('candidate'), 'promotion': {'approved': False,
                'reasons': ['insufficient evidence']}, **changes}

    def test_waiting_cycle_retains_last_completed_samples_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self.artifact(version='previous',cohort_ids=['a'])
            atomic_json(root/'models'/'candidate-previous.json',candidate)
            atomic_json(root/'models'/'completed-training.json',{
                'data_version':DATA_VERSION,'feature_version':FEATURE_VERSION,
                'validation_contract_version':VALIDATION_CONTRACT_VERSION,
                'candidate_version':'previous','cohort_ids':['a'],
                'completed_at':candidate['updated_at'],'training_decisions':4786})
            status = self.run_empty_cycle(root)
            self.assertEqual(status['completed_training_decisions'],4786)
            self.assertEqual(status['completed_training_at'],candidate['updated_at'])
            self.assertIsNone(status['training_decisions'])
            self.assertEqual(status['new_matches'],0)
        with tempfile.TemporaryDirectory() as directory:
            status = self.run_empty_cycle(Path(directory))
            self.assertIsNone(status['completed_training_decisions'])
            self.assertIsNone(status['completed_training_at'])

    def run_empty_cycle(self, root):
        def empty_dataset(*args, audit, **kwargs):
            audit.update(valid_training_decisions=0, source_eligible_decisions=0)
            return []
        with patch('service.model_training.constrain', return_value={'cpu_cores': 1, 'memory_mb': 256}), \
                patch('service.model_training.training_cohort_progress', return_value=self.progress([])), \
                patch('service.model_training.dataset', side_effect=empty_dataset), \
                patch('store.training_data.prospective_metrics', return_value={}):
            return run(root / 'ledger', 100, 1, 256)

    def test_empty_storage_waits_without_inventing_an_active_model(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            status = self.run_empty_cycle(root)
            self.assertEqual(status['state'], 'waiting')
            self.assertFalse(status['old_model_quarantined'])
            self.assertFalse((root / 'models' / 'active.json').exists())

    def test_sample_quality_does_not_become_full_archive_eligible_count(self):
        def sampled_dataset(*args, audit, **kwargs):
            audit.update(valid_training_decisions=None, source_eligible_decisions=None,
                         sample_valid_training_decisions=2, sample_examined_records=3,
                         counts_scope='sampled_qualified_inputs', sampled=True)
            return [sample_row(0), sample_row(0)]

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, \
                patch('service.model_training.constrain', return_value={}), \
                patch('service.model_training.training_cohort_progress',
                      return_value=self.progress([sample_row(i) for i in range(120)])), \
                patch('service.model_training.dataset', side_effect=sampled_dataset), \
                patch('store.training_data.prospective_metrics', return_value={}):
            status = run(Path(directory) / 'ledger', 100, 1, 256)
        self.assertEqual(status['eligible_matches'], 1)
        self.assertIsNone(status['eligible_decisions'])
        self.assertEqual(status['sampled_eligible_decisions'], 2)
        self.assertEqual(status['eligible_decisions_scope'], 'sampled_qualified_inputs')
        self.assertEqual(status['eligible_matches_scope'], 'sampled_qualified_inputs')
        self.assertEqual(status['data_quality']['sample_examined_records'], 3)

    def test_check_only_refreshes_progress_without_fitting(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            model = self.artifact()
            atomic_json(root / 'models' / 'candidate-1.json', model)
            rows = [{'match_id': 'm1'}, {'match_id': 'm2'}]
            with patch('service.model_training.constrain', return_value={'cpu_cores': 1, 'memory_mb': 256}), \
                    patch('service.model_training.dataset', return_value=rows) as load, \
                    patch('service.model_training.training_cohort_progress', return_value=self.progress(rows)), \
                    patch('service.model_training.train') as fit:
                status = run(root / 'ledger', 2, 1, 256, check_only=True)
            self.assertEqual(status['state'], 'candidate')
            self.assertEqual(status['new_matches'], 2)
            load.assert_not_called()
            fit.assert_not_called()

    def test_unverified_cohort_never_starts_full_dataset_or_training(self):
        rows = [sample_row(i) for i in range(120)]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, \
                patch('service.model_training.constrain', return_value={}), \
                patch('service.model_training.training_cohort_progress',
                      return_value=self.progress(rows, verified=False)), \
                patch('service.model_training.dataset') as load, \
                patch('service.model_training.train') as fit:
            status = run(Path(directory) / 'ledger', 1, 1, 256)
        self.assertFalse(status['cycle_ready'])
        self.assertIsNone(status['new_matches'])
        load.assert_not_called()
        fit.assert_not_called()

    def test_completed_unpromoted_candidate_prevents_repeat_with_older_active(self):
        rows = [sample_row(i) for i in range(120)]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            old = self.artifact(version='old-active', promotion=self.approval(),
                                cohort_ids=['old'], updated_at='2024-01-01T00:00:00+00:00')
            atomic_json(root / 'models' / 'active.json', old)
            candidate = self.artifact(version='new-candidate', cohort_ids=[str(i) for i in range(120)],
                                      updated_at='2025-01-01T00:00:00+00:00')
            atomic_json(root / 'models' / 'candidate-new.json', candidate)
            with patch('service.model_training.constrain', return_value={}), \
                    patch('service.model_training.training_cohort_progress', return_value=self.progress(rows)), \
                    patch('service.model_training.dataset') as load:
                status = run(root / 'ledger', 1, 1, 256)
            self.assertEqual(status['new_matches'], 0)
            self.assertEqual(status['last_completed_version'], 'new-candidate')
            self.assertEqual(status['state'], 'candidate')
            self.assertEqual(json.loads((root / 'models' / 'active.json').read_text()), old)
            load.assert_not_called()

    def test_candidate_commit_recovers_watermark_after_crash_before_watermark_write(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            model_root = Path(directory) / 'models'
            old = self.artifact(version='v1', cohort_ids=['a'], promotion=self.approval())
            latest = self.artifact(version='v2', cohort_ids=['b'], completed_cohort_ids=['a', 'b'],
                                   updated_at='2025-01-02T00:00:00+00:00')
            atomic_json(model_root / 'completed-training.json', {
                'data_version': DATA_VERSION, 'feature_version': FEATURE_VERSION,
                'validation_contract_version': VALIDATION_CONTRACT_VERSION,
                'candidate_version': 'v1', 'completed_at': old['updated_at'], 'cohort_ids': ['a']})
            completed = _completed_cohort(model_root, [old, latest])
        self.assertEqual(completed['cohort_ids'], ['a', 'b'])
        self.assertEqual(completed['candidate_version'], 'v2')

    def test_failed_candidate_write_does_not_consume_training_cohort(self):
        rows = [sample_row(i) for i in range(120)]
        def fail_candidate(path, payload):
            if path.name.startswith('candidate-'):
                raise OSError('disk full')
            atomic_json(path, payload)
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, \
                patch('service.model_training.constrain', return_value={}), \
                patch('service.model_training.training_cohort_progress', return_value=self.progress(rows)), \
                patch('service.model_training.dataset', return_value=rows), \
                patch('service.model_training.atomic_json', side_effect=fail_candidate):
            root = Path(directory)
            with self.assertRaisesRegex(OSError, 'disk full'):
                run(root / 'ledger', 100, 1, 256)
            self.assertFalse((root / 'models' / 'completed-training.json').exists())
            self.assertFalse(list((root / 'models').glob('candidate-*.json')))

    def test_inputs_arriving_during_training_remain_for_next_cycle(self):
        rows = [sample_row(i) for i in range(120)]
        extra = sample_row(120)
        first_progress = self.progress(rows)
        later_progress = self.progress([*rows, extra])
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, \
                patch('service.model_training.constrain', return_value={}), \
                patch('service.model_training.training_cohort_progress',
                      side_effect=[first_progress, later_progress, later_progress]), \
                patch('service.model_training.dataset', return_value=[*rows, extra]) as load:
            root = Path(directory)
            status = run(root / 'ledger', 100, 1, 256)
            checkpoint = json.loads((root / 'models' / 'completed-training.json').read_text())
            candidate = json.loads(next((root / 'models').glob('candidate-*.json')).read_text())
            self.assertNotIn(extra['match_id'], candidate['cohort_ids'])
            self.assertNotIn(extra['match_id'], checkpoint['cohort_ids'])
            self.assertEqual(status['new_matches'], 1)
            self.assertEqual(load.call_args.kwargs['cohort_labels'], first_progress['qualified_labels'])
            next_status = run(root / 'ledger', 100, 1, 256, check_only=True)
            self.assertEqual(next_status['new_matches'], 1)
            self.assertEqual(load.call_count, 1)

    def test_post_training_progress_failure_preserves_completion_and_unknown_pending_count(self):
        rows = [sample_row(i) for i in range(120)]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, \
                patch('service.model_training.constrain', return_value={}), \
                patch('service.model_training.training_cohort_progress',
                      side_effect=[self.progress(rows), OSError('read unavailable')]), \
                patch('service.model_training.dataset', return_value=rows):
            root = Path(directory)
            status = run(root / 'ledger', 100, 1, 256)
            self.assertTrue((root / 'models' / 'completed-training.json').exists())
        self.assertIsNone(status['new_matches'])
        self.assertFalse(status['pending_cohort_verified'])
        self.assertEqual(status['progress_error'], 'read unavailable')

    def test_global_calibration_skips_push_context_without_removing_prediction_context(self):
        rows = [sample_row(i) for i in range(120)]
        context = [{**row, 'y': None, 'graded': False, 'observation_weight': 0.,
                    'outcome': 'under', 'market': 'OU', 'line': '2'} for row in rows]
        with patch('service.model_training.fit_logistic',
                   return_value={'method': 'identity', 'samples': 40}) as calibrator:
            _, _, calibration = rolling_validation([*rows, *context])
        self.assertEqual(len(calibrator.call_args.args[0]), 40)
        self.assertEqual(calibration['excluded_context_rows'], 40)

    def test_rollback_rejects_legacy_approval_and_preserves_source(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            model_root = Path(directory) / 'models'
            old = self.artifact(version='legacy', promotion={'approved': True})
            atomic_json(model_root / 'active-backup-legacy.json', old)
            self.assertFalse(rollback_active_model(model_root, 'legacy')['restored'])
            self.assertIsNone(backup_active_model(model_root, model_root / 'active-backup-legacy.json'))
            current = self.artifact(version='current', promotion=self.approval())
            atomic_json(model_root / 'active-backup-current.json', current)
            self.assertTrue(rollback_active_model(model_root, 'current')['restored'])
            self.assertEqual(json.loads((model_root / 'active.json').read_text()), current)
            self.assertEqual(json.loads((model_root / 'active-backup-legacy.json').read_text()), old)

    def test_compatible_candidate_is_retained_without_promotion(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            path = root / 'models' / 'candidate-1.json'
            model = self.artifact()
            atomic_json(path, model)
            status = self.run_empty_cycle(root)
            self.assertEqual(status['state'], 'candidate')
            self.assertEqual(status['candidate_version'], model['version'])
            self.assertEqual(json.loads(path.read_text()), model)
            self.assertFalse((path.parent / 'active.json').exists())

    def test_legacy_candidate_is_preserved_without_seeding_training_cohort(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            path = root / 'models' / 'candidate-1.json'
            model = self.artifact(data_version='legacy-v1')
            atomic_json(path, model)
            status = self.run_empty_cycle(root)
            self.assertEqual(status['state'], 'waiting')
            self.assertTrue(status['old_model_quarantined'])
            self.assertEqual(json.loads(path.read_text()), model)
            self.assertEqual(json.loads((path.parent / 'quarantined-candidate-1.json').read_text()), model)
            self.assertFalse((path.parent / 'active.json').exists())

    def test_legacy_active_is_preserved_and_unavailable_for_execution(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            path = root / 'models' / 'active.json'
            model = self.artifact(feature_version='legacy')
            atomic_json(path, model)
            status = self.run_empty_cycle(root)
            self.assertEqual(status['state'], 'waiting')
            self.assertTrue(status['old_model_quarantined'])
            self.assertEqual(json.loads(path.read_text()), model)
            self.assertEqual(json.loads((path.parent / 'quarantined-active.json').read_text()), model)
