from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from core.learned_model import DATA_VERSION, FEATURES
from core.decision_features import FEATURE_VERSION
from service.live_expert import LiveExpertService
from service.model_service import ModelService
from service.model_training import atomic_json
from service.runtime_settings import RuntimeConfig, RuntimeSettings, validated
from tests import test_live_expert


class LearnedDecisionTests(unittest.TestCase):
    def test_setting_is_validated_and_persisted(self):
        with self.assertRaises(ValueError):
            validated(RuntimeConfig(), {'model_shadow_enabled':1})
        with tempfile.TemporaryDirectory() as directory:
            settings = RuntimeSettings()
            settings.bind(directory+"/ledger")
            settings.update({'model_shadow_enabled':False}, 1, lambda *_: None)
            worker = ModelService()
            worker.bind(directory+'/ledger')
            worker._reload_settings()
            self.assertEqual(worker.status()['mode'], 'active')
            self.assertFalse(worker.cfg.betting_enabled)

    def test_real_switch_changes_probability_and_keeps_training_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'models').mkdir()
            weights = [0.] * len(FEATURES)
            weights[0] = 2
            model = {'version':'test','data_version':DATA_VERSION,'features':list(FEATURES),
                     'weights':weights,'updated_at':'2025-01-01T00:00:00+00:00',
                     'feature_version':FEATURE_VERSION,
                     'validation':{'brier':.1},'baseline':{'brier':.2}}
            atomic_json(root/'models'/'active.json', model)
            hub = test_live_expert.LiveExpertTests().hub()
            svc = LiveExpertService()
            snapshot = {**hub.decision_snapshot('m'), '_model_path':str(root/'models'/'active.json')}
            shadow = svc._calculate(snapshot)
            svc.configure(replace(RuntimeConfig(), model_shadow_enabled=False), 2)
            active = svc._calculate(snapshot)
            self.assertTrue(active['ensemble']['model_application']['applied'], active['ensemble']['model_application'])
            self.assertEqual(active['baseline_ensemble']['candidates'], shadow['ensemble']['candidates'])
            self.assertGreater(max(c['p_model'] for c in active['ensemble']['candidates']), .5)
            self.assertTrue(any(a['p_model'] != b['p_model'] for a, b in zip(
                active['ensemble']['candidates'], active['baseline_ensemble']['candidates'])))
            self.assertFalse(svc.config.betting_enabled)
            model['updated_at'] = '2099-01-01T00:00:00+00:00'
            atomic_json(root/'models'/'active.json', model)
            future = svc._calculate(snapshot)
            self.assertFalse(future['ensemble']['model_application']['applied'])
            self.assertEqual(future['ensemble']['candidates'], future['baseline_ensemble']['candidates'])

    def test_missing_model_falls_back_without_halting_live_calculation(self):
        svc = LiveExpertService()
        svc.configure(replace(RuntimeConfig(), model_shadow_enabled=False), 2)
        hub = test_live_expert.LiveExpertTests().hub()
        row = svc._calculate(hub.decision_snapshot('m'))
        self.assertFalse(row['ensemble']['model_application']['applied'])
        self.assertEqual(row['ensemble']['candidates'], row['baseline_ensemble']['candidates'])
