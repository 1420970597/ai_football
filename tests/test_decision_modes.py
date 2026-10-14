"""Explicit live routes, approved artifacts and route-specific persistence."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from api.app import ApiApp
from core.learned_model import FEATURES
from core.live_model import MODEL_VERSION
from service.analysis import AnalysisConfig, AnalysisService
from service.ledger import DecisionLedger, LedgerEntry, recommendation_evidence_key
from service.live_expert import LiveExpertService
from service.model_service import ModelService
from service.model_training import atomic_json
from service.runtime_settings import RuntimeConfig, RuntimeSettings, validated
from tests import test_live_expert
from tests.test_model_validation import approved_artifact


class DecisionModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'models' / 'active.json'
        self.hub = test_live_expert.LiveExpertTests().hub()
        self.snapshot = {**self.hub.decision_snapshot('m'), '_model_path': str(self.path)}

    def artifact(self, **changes):
        weights = [0.] * len(FEATURES)
        weights[0] = 2.
        return approved_artifact('approved-test', **{'weights': weights, **changes})

    def calculate(self, mode='algorithm', weight=.5):
        service = LiveExpertService()
        service.configure(replace(RuntimeConfig(), decision_mode=mode, model_blend_weight=weight), 2)
        return service._calculate(self.snapshot)

    def test_settings_validate_persist_and_reload_independently_from_shadow(self):
        for patch in ({'decision_mode': 'llm'}, {'decision_mode': True},
                      {'model_blend_weight': -1}, {'model_blend_weight': 1.01},
                      {'model_blend_weight': float('nan')}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validated(RuntimeConfig(), patch)
        settings = RuntimeSettings()
        settings.bind(str(self.root / 'ledger'))
        settings.update({'decision_mode': 'hybrid', 'model_blend_weight': .35}, 1, lambda *_: None)
        worker = ModelService()
        worker.bind(str(self.root / 'ledger'))
        worker._reload_settings()
        self.assertEqual(worker.status()['decision_mode'], 'hybrid')
        self.assertEqual(worker.cfg.model_blend_weight, .35)
        self.assertEqual(worker.status()['mode'], 'shadow')

    def test_algorithm_does_not_load_model_or_require_an_artifact(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{broken')
        row = self.calculate()
        self.assertEqual(row['decision_mode_status'], 'ready')
        self.assertTrue(row['ensemble']['model_application']['execution_enabled'])
        self.assertFalse(row['ensemble']['model_application']['applied'])
        self.assertTrue(row['candidates'])
        baseline = {(c['market'], c['line'], c['outcome']): c['p_model']
                    for c in row['baseline_ensemble']['candidates']}
        for c in row['candidates']:
            self.assertEqual(c['p_model'], baseline[(c['market'], c['line'], c['outcome'])])
            self.assertIsNone(c['learned_probability'])

    def test_approved_model_and_hybrid_blend_are_coherent_and_auditable(self):
        atomic_json(self.path, self.artifact())
        algorithm, model = self.calculate(), self.calculate('model')
        algorithm_p = {(c['market'], c['line'], c['outcome']): c['p_model'] for c in algorithm['candidates']}
        model_p = {(c['market'], c['line'], c['outcome']): c['p_model'] for c in model['candidates']}
        self.assertTrue(any(model_p[key] != value for key, value in algorithm_p.items()))
        for weight in (0., .35, 1.):
            hybrid = self.calculate('hybrid', weight)
            self.assertTrue(hybrid['ensemble']['model_application']['applied'])
            self.assertTrue(hybrid['ensemble']['model_application']['execution_enabled'])
            self.assertEqual(hybrid['baseline_ensemble'], algorithm['baseline_ensemble'])
            self.assertEqual(hybrid['decision_model_version'], 'approved-test')
            groups = {}
            for c in hybrid['candidates']:
                key = (c['market'], c['line'], c['outcome'])
                self.assertAlmostEqual(c['p_model'], algorithm_p[key] * (1-weight) + model_p[key] * weight, places=6)
                self.assertEqual(c['decision_evidence']['model_version'], 'approved-test')
                self.assertEqual(c['decision_evidence']['input_cutoff_ms'], self.snapshot['captured_at_ms'])
                self.assertEqual(c['decision_evidence']['weights']['model'], weight)
                groups.setdefault(key[:2], []).append(c)
            for candidates in groups.values():
                self.assertAlmostEqual(sum(c['p_model'] for c in candidates), 1., places=5)
            self.assertLessEqual(hybrid['ensemble']['total_exposure'], RuntimeConfig().max_total_exposure)

    def test_missing_future_unapproved_corrupt_and_legacy_models_are_unavailable(self):
        artifacts = [None, self.artifact(promotion={'approved': False}), self.artifact(promotion=None),
                     self.artifact(data_version='legacy-v1'), self.artifact(feature_version='legacy'),
                     self.artifact(weights=[0.]), self.artifact(updated_at='2099-01-01T00:00:00+00:00'),
                     self.artifact(updated_at='2025-01-01T00:00:00'), {'corrupt': True}]
        for artifact in artifacts:
            if artifact is not None:
                atomic_json(self.path, artifact)
            for mode in ('model', 'hybrid'):
                with self.subTest(artifact=artifact, mode=mode):
                    row = self.calculate(mode)
                    self.assertEqual(row['decision'], 'unavailable')
                    self.assertFalse(row['has_buy'])
                    self.assertEqual(row['picks'], [])
                    self.assertEqual(row['forecasts'], [])
                    self.assertTrue(row['baseline_ensemble']['candidates'])
                    self.assertFalse(row['ensemble']['model_application']['execution_enabled'])
        self.path.write_text('{broken')
        self.assertEqual(self.calculate('model')['decision'], 'unavailable')
        self.path.write_text(json.dumps(self.artifact(weights=[float('nan')] * len(FEATURES))))
        self.assertEqual(self.calculate('model')['decision'], 'unavailable')

    def test_unavailable_model_discards_retained_recommendation_and_switch_clears_results(self):
        service = LiveExpertService()
        service._recommendations['m'] = {'decision_mode': 'algorithm', 'model_version': '', 'picks': [{}]}
        service._remember_recommendation(self.calculate('model'))
        self.assertNotIn('m', service._recommendations)
        service._recommendations['m'] = {'picks': [{}]}
        service.configure(replace(RuntimeConfig(), decision_mode='hybrid'), 3)
        self.assertFalse(service._recommendations)

    def test_legacy_approval_is_unavailable_in_both_routes_and_preserved(self):
        cases = [self.artifact(validation_contract_version='legacy-contract'),
                 self.artifact(promotion={'approved': True})]
        missing = self.artifact()
        missing.pop('validation_contract_version')
        cases.append(missing)
        for artifact in cases:
            atomic_json(self.path, artifact)
            for mode in ('model', 'hybrid'):
                with self.subTest(artifact=artifact, mode=mode):
                    row = self.calculate(mode)
                    self.assertEqual(row['decision'], 'unavailable')
                    self.assertFalse(row['ensemble']['model_application']['execution_enabled'])
                    self.assertTrue(row['baseline_ensemble']['candidates'])
                    self.assertEqual(json.loads(self.path.read_text()), artifact)

    def test_ledger_records_components_and_separates_model_and_blend_identities(self):
        atomic_json(self.path, self.artifact())
        ledger = DecisionLedger(str(self.root / 'ledger'))
        identities = []
        for mode, weight in (('model', 1.), ('hybrid', .35), ('hybrid', .65)):
            row = self.calculate(mode, weight)
            source = {**row, 'forecast': row['forecasts'][0], 'record_kind': 'recommendation'}
            entry = ledger._live_entry(source)
            identities.append(entry.decision_id)
            ledger.record_live(source)
        self.assertEqual(len(set(identities)), 3)
        entries = ledger.load()
        self.assertEqual(len(entries), 3)
        for entry in entries:
            self.assertIsNotNone(entry.learned_probability)
            self.assertIsNotNone(entry.algorithm_probability)
            self.assertEqual(entry.decision_model_version, 'approved-test')
            self.assertEqual(entry.input_cutoff_ms, self.snapshot['captured_at_ms'])
            self.assertTrue(entry.model_application['applied'])

    def test_performance_evidence_never_borrows_another_mode_or_blend(self):
        ledger = DecisionLedger(str(self.root / 'ledger'))
        common = dict(at='2026-01-01T12:00:00+00:00', market='OU', line='2.25', outcome='over',
                      odds=2., is_pick=True, algorithm='economic_ensemble', trigger='live_recommendation',
                      model_version=MODEL_VERSION, decision_model_version='approved-test')
        for identity, mode, weight, status in (('m1', 'model', 1., 'won'),
                ('m2', 'hybrid', .35, 'lost'), ('m3', 'hybrid', .65, 'won')):
            ledger._append([LedgerEntry(**common, match_id=identity, decision_id=identity,
                decision_mode=mode, blend_weights={'model': weight}, status=status,
                pnl=1. if status == 'won' else -1.)])
        for mode, weight, hits in (('model', 1., 1), ('hybrid', .35, 0), ('hybrid', .65, 1)):
            result = ledger.recommendation_performance('OU', '2.25', 'over',
                decision_model_version='approved-test', decision_mode=mode, model_blend_weight=weight)
            self.assertEqual(result['hit_count'], hits)
            self.assertEqual(result['independent_matches'], 1)
        keys = [recommendation_evidence_key({**common, 'decision_mode': 'hybrid',
                                            'blend_weights': {'model': weight}}) for weight in (.35, .65)]
        results = ledger.recommendation_performance_many(keys)
        self.assertEqual(results[keys[0]]['hit_count'], 0)
        self.assertEqual(results[keys[1]]['hit_count'], 1)
        legacy_key = ('economic_ensemble', 'OU', '2.25', 'over', 'approved-test')
        mixed = ledger.recommendation_performance_many([legacy_key, *keys])
        self.assertEqual(mixed[legacy_key]['hit_count'], 2)
        self.assertEqual(mixed[legacy_key]['independent_matches'], 3)
        self.assertEqual(mixed[keys[0]]['hit_count'], 0)

    def test_list_detail_and_buy_api_cannot_restore_old_route_when_model_unavailable(self):
        service = AnalysisService(MagicMock(), realtime=self.hub,
            config=AnalysisConfig(use_llm=False, ledger_root=str(self.root / 'ledger')))
        service.live_expert.configure(replace(RuntimeConfig(), decision_mode='model'), 2)
        service.live_expert.compute(self.snapshot, self.hub)
        service.ledger.live_recommendations = MagicMock(return_value={'m': [{
            'market': 'OU', 'line': '2.25', 'outcome': 'over', 'odds': 1.9,
            'p_model': .8, 'decision_mode': 'algorithm', 'config_version': 2}]})
        app = ApiApp(MagicMock(), analysis=service)
        for path in ('/api/v1/workbench', '/api/v1/workbench/m', '/api/v1/recommendations'):
            code, payload = app.dispatch('GET', path, {}, {})
            self.assertEqual(code, 200, payload)
            if path.endswith('recommendations'):
                self.assertEqual(payload['open'], [])
            else:
                row = payload['matches'][0] if path.endswith('workbench') else payload
                self.assertEqual(row['picks'], [])
                self.assertEqual(row['decision'], 'unavailable')

    def test_retained_filter_checks_mode_artifact_settings_and_weight(self):
        atomic_json(self.path, self.artifact())
        row = self.calculate('hybrid', .35)
        candidate = row['forecasts'][0]
        self.assertTrue(ApiApp._same_decision_route(row, candidate))
        for patch in ({'decision_mode': 'model'}, {'decision_model_version': 'old'},
                      {'blend_weights': {'algorithm': .5, 'model': .5}}, {'config_version': 1}):
            self.assertFalse(ApiApp._same_decision_route(row, {**candidate, **patch}))
