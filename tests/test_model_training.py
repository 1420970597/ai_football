"""Training lifecycle regressions without changing test-process resources."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.decision_features import FEATURE_VERSION
from core.learned_model import DATA_VERSION, FEATURES
from service.model_training import atomic_json, rolling_validation, run
from tests.test_learned_model import sample_row


class ModelTrainingLifecycleTests(unittest.TestCase):
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

    def test_promoted_artifact_is_the_independently_scored_final_fold(self):
        from core.model_validation import evaluate
        from core.learned_model import predict
        from core.oof_peer import calibrate_peer_vector
        from core.probability_calibration import calibrate_probability
        rows = [sample_row(i) for i in range(120)]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, \
                patch('service.model_training.constrain', return_value={}), \
                patch('service.model_training.dataset', return_value=rows), \
                patch('service.model_training.promotion', return_value={'approved': True, 'reasons': []}):
            root = Path(directory)
            status = run(root / 'ledger', 100, 1, 256)
            model = json.loads((root / 'models' / 'active.json').read_text())
        self.assertEqual(status['state'], 'ready')
        test_rows = [row for row in rows if row['match_id'] in model['validation_match_ids']]
        probabilities = [calibrate_probability(predict(model['weights'],
            calibrate_peer_vector(row['x'], model['peer_calibration'])), model['calibration'])
            for row in test_rows]
        self.assertEqual(model['rolling_validation'][-1]['model_calibrated'], evaluate(test_rows, probabilities))
        self.assertEqual(model['validation'], evaluate(test_rows, probabilities))
        self.assertIn('validation_uncalibrated', model)
        self.assertEqual(model['training_epochs'], model['rolling_validation'][-1]['training_epochs'])

    def artifact(self, **changes):
        return {'version': 'candidate', 'data_version': DATA_VERSION, 'feature_version': FEATURE_VERSION,
                'features': list(FEATURES), 'weights': [0.] * len(FEATURES),
                'promotion': {'approved': False, 'reasons': ['insufficient evidence']}, **changes}

    def run_empty_cycle(self, root):
        def empty_dataset(*args, audit, **kwargs):
            audit.update(valid_training_decisions=0, source_eligible_decisions=0)
            return []
        with patch('service.model_training.constrain', return_value={'cpu_cores': 1, 'memory_mb': 256}), \
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
            return [{'match_id': 'm1'}, {'match_id': 'm1'}]

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, \
                patch('service.model_training.constrain', return_value={}), \
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
                    patch('service.model_training.dataset', return_value=rows), \
                    patch('service.model_training.train') as fit:
                status = run(root / 'ledger', 2, 1, 256, check_only=True)
            self.assertEqual(status['state'], 'candidate')
            self.assertEqual(status['new_matches'], 2)
            fit.assert_not_called()

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

    def test_legacy_candidate_without_active_is_quarantined_without_unlink_failure(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            path = root / 'models' / 'candidate-1.json'
            model = self.artifact(data_version='legacy-v1')
            atomic_json(path, model)
            status = self.run_empty_cycle(root)
            self.assertEqual(status['state'], 'waiting')
            self.assertTrue(status['old_model_quarantined'])
            self.assertEqual(json.loads(path.read_text()), model)
            self.assertEqual(json.loads((path.parent / 'quarantined-active.json').read_text()), model)
            self.assertFalse((path.parent / 'active.json').exists())

    def test_legacy_active_is_preserved_in_quarantine_and_removed_from_active_slot(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            path = root / 'models' / 'active.json'
            model = self.artifact(feature_version='legacy')
            atomic_json(path, model)
            status = self.run_empty_cycle(root)
            self.assertEqual(status['state'], 'waiting')
            self.assertTrue(status['old_model_quarantined'])
            self.assertFalse(path.exists())
            self.assertEqual(json.loads((path.parent / 'quarantined-active.json').read_text()), model)
