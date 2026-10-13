from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.learned_model import DATA_VERSION, FEATURES
from core.decision_features import FEATURE_VERSION
from service.live_expert import LiveExpertService
from service.model_service import ModelService
from service.model_training import atomic_json
from service.runtime_settings import RuntimeConfig, RuntimeSettings, validated
from tests import test_live_expert


class LearnedDecisionTests(unittest.TestCase):
    def test_live_model_and_shadow_projection_match_and_hybrid_keeps_formula(self):
        from core.learned_decision import calibrate
        from core.model_validation import coherent_probabilities
        from core.live_model import MODEL_VERSION
        learned = [.4, .6, .7, .3]
        algorithm = [.6, .4, .3, .7]
        candidates = [
            {'match_id': 'm', 'at': '2025-01-02T00:00:00+00:00',
             'algorithm': 'economic_ensemble', 'market': 'OU', 'line': line,
             'outcome': outcome, 'p_model': probability, 'p_market': probability,
             'odds': 2., 'win_stake': .5, 'loss_stake': .5, 'research_only': False,
             'tail': 0., 'training_history': []}
            for (line, outcome), probability in zip(
                [('2.5', 'over'), ('2.5', 'under'), ('3.5', 'over'), ('3.5', 'under')], algorithm)]
        model = {'version': 'projection-test', 'data_version': DATA_VERSION,
                 'feature_version': FEATURE_VERSION, 'features': list(FEATURES),
                 'weights': [0.] * len(FEATURES),
                 'updated_at': '2025-01-01T00:00:00+00:00',
                 'promotion': {'approved': True}}
        expected = coherent_probabilities(candidates, learned)
        self.assertEqual([round(p, 6) for p in expected], [.55, .45, .55, .45])
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / 'active.json'
            atomic_json(path, model)
            snapshot = {'_model_path': str(path), 'captured_at_ms': 1735776000000.,
                        'score': [0, 0], 'status': {'mst': 1200, 'mmp': '6'}}
            for mode, weight in [('model', 1.), ('hybrid', .25)]:
                with self.subTest(mode=mode), \
                        patch('core.learned_decision._load', return_value=model), \
                        patch('core.learned_decision.predict', side_effect=learned):
                    result = calibrate({'candidates': candidates}, [], snapshot,
                                       RuntimeConfig(), MODEL_VERSION, mode=mode, blend_weight=weight)
                    self.assertTrue(result['model_application']['applied'], result['model_application'])
                    for candidate, raw_algorithm, projected in zip(result['candidates'], algorithm, expected):
                        self.assertAlmostEqual(candidate['learned_probability'], projected)
                        self.assertAlmostEqual(candidate['p_model'],
                                               (1 - weight) * raw_algorithm + weight * projected)
                        self.assertEqual(candidate['decision_evidence']['final_probability'], candidate['p_model'])

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
                     'validation':{'brier':.1},'baseline':{'brier':.2},'promotion':{'approved':True}}
            atomic_json(root/'models'/'active.json', model)
            hub = test_live_expert.LiveExpertTests().hub()
            svc = LiveExpertService()
            snapshot = {**hub.decision_snapshot('m'), '_model_path':str(root/'models'/'active.json')}
            shadow = svc._calculate(snapshot)
            svc.configure(replace(RuntimeConfig(), model_shadow_enabled=False, decision_mode='model'), 2)
            active = svc._calculate(snapshot)
            self.assertTrue(active['ensemble']['model_application']['applied'], active['ensemble']['model_application'])
            self.assertEqual(active['baseline_ensemble']['candidates'], shadow['baseline_ensemble']['candidates'])
            self.assertGreater(max(c['p_model'] for c in active['ensemble']['candidates']), .5)
            self.assertTrue(any(a['p_model'] != b['p_model'] for a, b in zip(
                active['ensemble']['candidates'], active['baseline_ensemble']['candidates'])))
            self.assertFalse(svc.config.betting_enabled)
            model['updated_at'] = '2099-01-01T00:00:00+00:00'
            atomic_json(root/'models'/'active.json', model)
            future = svc._calculate(snapshot)
            self.assertFalse(future['ensemble']['model_application']['applied'])
            self.assertEqual(future['ensemble']['candidates'], [])

    def test_missing_model_does_not_halt_algorithm_calculation(self):
        svc = LiveExpertService()
        svc.configure(replace(RuntimeConfig(), model_shadow_enabled=False), 2)
        hub = test_live_expert.LiveExpertTests().hub()
        row = svc._calculate(hub.decision_snapshot('m'))
        self.assertFalse(row['ensemble']['model_application']['applied'])
        self.assertEqual([c['p_model'] for c in row['ensemble']['candidates']],
                         [c['p_model'] for c in row['baseline_ensemble']['candidates']])
