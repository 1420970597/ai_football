"""Training lifecycle regressions without changing test-process resources."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.decision_features import FEATURE_VERSION
from core.learned_model import DATA_VERSION, FEATURES
from service.model_training import atomic_json, run


class ModelTrainingLifecycleTests(unittest.TestCase):
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
