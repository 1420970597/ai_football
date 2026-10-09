import json
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from api.app import ApiApp
from collector.leyu_client import MarketQuote, OddsQuote
from collector.leyu_realtime import PriceTick
from service.analysis import AnalysisConfig, AnalysisService
from service.ledger import DecisionLedger
from service.runtime_settings import RuntimeSettings, VersionConflict
from tests import test_live_expert


class RuntimeSettingsTests(unittest.TestCase):
    def test_atomic_validation_secret_persistence_and_restore(self):
        with tempfile.TemporaryDirectory() as root:
            settings = RuntimeSettings()
            settings.bind(root + '/ledger')
            callback = MagicMock()
            out = settings.update({'llm_api_key': 'test-only-key', 'min_ev': .03}, 1, callback)
            self.assertTrue(out['settings']['has_llm_key'])
            self.assertNotIn('test-only-key', json.dumps(out))
            self.assertEqual(settings.path.stat().st_mode & 0o777, 0o600)
            restored = RuntimeSettings()
            restored.bind(root + '/ledger')
            self.assertEqual(restored.version, 2)
            self.assertEqual(restored.config.min_ev, .03)
            self.assertEqual(restored.config.llm_api_key, 'test-only-key')
            before = settings.path.read_bytes()
            for invalid in ({'min_ev': -1}, {'min_ev': float('nan')}, {'min_probability': True},
                            {'algorithms': []}, {'primary_algorithm': 'fake'}, {'devig_method': 'fake'},
                            {'llm_enabled': 'yes'}, {'llm_base_url': 'file:///etc/passwd'},
                            {'llm_base_url': 'https://user:password@example.org/v1'}, {'llm_max_tokens': 256.5},
                            {'llm_enabled': True}, {'unknown': 1}):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    settings.update(invalid, 2, callback)
            self.assertEqual(settings.path.read_bytes(), before)
            self.assertEqual(settings.version, 2)
            callback.assert_called_once()
            with self.assertRaises(VersionConflict):
                settings.update({}, 1, callback)

    def test_write_failure_keeps_current_settings(self):
        with tempfile.TemporaryDirectory() as root:
            settings = RuntimeSettings()
            settings.bind(root + '/ledger')
            with patch('service.runtime_settings.os.replace', side_effect=OSError('read only')):
                with self.assertRaises(ValueError):
                    settings.update({'min_ev': .2}, 1, MagicMock())
            self.assertEqual(settings.version, 1)
            self.assertEqual(settings.config.min_ev, .02)

    def test_parallel_edit_has_one_winner(self):
        settings = RuntimeSettings()
        outcomes = []
        def save():
            try:
                settings.update({'min_ev': .2}, 1, lambda *_: None)
                outcomes.append('ok')
            except VersionConflict:
                outcomes.append('conflict')
        threads = [threading.Thread(target=save) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertCountEqual(outcomes, ['ok', 'conflict'])

    def test_hot_configuration_changes_actual_results_and_api_conflicts(self):
        hub = test_live_expert.LiveExpertTests().hub()
        ts = int(time.time() * 1000)
        hub._record_ticks([PriceTick('m', '2', '', '3.25', oc, oc, odds, odds, ts)
                           for oc, odds in [('Over', 10), ('Under', 1.98)]])
        hub._subscribed = ['m']
        svc = AnalysisService(MagicMock(), realtime=hub, config=AnalysisConfig(use_llm=False))
        app = ApiApp(MagicMock(), analysis=svc)
        code, out = app.dispatch('POST', '/api/v1/settings', {}, {'version': 1, 'settings': {
            'algorithms': ['poisson_market'], 'primary_algorithm': 'poisson_market', 'min_ev': 0, 'min_probability': 0}})
        self.assertEqual(code, 200)
        self.assertEqual(out['version'], 2)
        self.assertIn('m', svc._pending)
        row = svc.decide_matches(['m'])['decisions'][0]
        self.assertEqual(row['config_version'], 2)
        self.assertEqual(len(row['evaluations']), 1)
        self.assertTrue(row['picks'])
        svc.update_settings({'min_ev': 1, 'devig_method': 'power'}, 2)
        new = svc.decide_matches(['m'])['decisions'][0]
        self.assertFalse(new['picks'])
        self.assertNotEqual(row['markets'][0]['quotes'][0]['p_market'], new['markets'][0]['quotes'][0]['p_market'])
        self.assertEqual(app.dispatch('POST', '/api/v1/settings', {}, {'version': 1, 'settings': {}})[0], 409)
        self.assertEqual(app.dispatch('POST', '/api/v1/settings', {}, {'version': 3, 'settings': {'min_ev': -1}})[0], 400)


class CoverageAndProspectiveTests(unittest.TestCase):
    def test_all_lines_half_unknown_incomplete_and_no_rest_price_rollback(self):
        hub = test_live_expert.LiveExpertTests().hub()
        ts = int(time.time() * 1000)
        ticks = [PriceTick('m', '2', '', str(i / 4), oc, oc, 1.95, 1.95, ts)
                 for i in range(1, 241) for oc in ('Over', 'Under')]
        ticks += [PriceTick('m', '18', '', '1.5', 'Over', 'Over', 2.1, 2.1, ts),
                  PriceTick('m', '999', '', '', 'yes', 'yes', 2, 2, ts)]
        hub._record_ticks(ticks)
        svc = AnalysisService(MagicMock(), realtime=hub, config=AnalysisConfig(use_llm=False))
        row = svc.decide_matches(['m'])['decisions'][0]
        self.assertGreater(len(row['markets']), 240)
        half = next(m for m in row['markets'] if m['market'] == 'OU_1H')
        self.assertFalse(half['complete'])
        self.assertEqual(half['missing_outcomes'], ['under'])
        self.assertIsNone(half['quotes'][0]['p_market'])
        self.assertTrue(any(m['market'] == 'RAW_999' for m in row['markets']))
        code, public = ApiApp(MagicMock(), analysis=svc).dispatch('GET', '/api/v1/workbench', {}, {})
        self.assertEqual(code, 200)
        self.assertEqual(len(public['matches'][0]['markets']), len(row['markets']))
        hub.live.upsert_many([PriceTick('m', '18', '', '1.5', 'Over', 'Over', 1.2, 1.2, ts - 10000)])
        self.assertEqual(next(q.odds for q in hub.live.book('m') if q.chpid == '18'), 2.1)

    def test_rest_added_quotes_and_raw_metadata_enter_realtime_book(self):
        hub = test_live_expert.LiveExpertTests().hub()
        match = MagicMock()
        match.mid='m'
        match.tournament='真实足球联赛'
        match.home='甲'
        match.away='乙'
        match.sport='足球'
        match.sport_id='1'
        match.is_finished=False
        match.markets=[MarketQuote('998', '双方进球', 0, '', (
            OddsQuote('a', 'other', '是', 2.1, ctsp=int(time.time()*1000)),))]
        hub.seed_matches([match])
        self.assertTrue(any(q.chpid == '998' and q.ot == '是' for q in hub.live.book('m')))
        self.assertEqual(hub.decision_snapshot('m')['market_meta']['998']['name'], '双方进球')

    def test_first_direction_freezes_survives_restart_and_grades(self):
        with tempfile.TemporaryDirectory() as root:
            hub = test_live_expert.LiveExpertTests().hub()
            svc = AnalysisService(MagicMock(), realtime=hub, config=AnalysisConfig(use_llm=False, ledger_root=root + '/ledger'))
            row = svc.decide_matches(['m'])['decisions'][0]
            self.assertEqual(svc.ledger.load(), [])  # writer is outside the fast path
            self.assertEqual(svc.live_expert.flush_decisions(), 4)
            original = svc.ledger.load()
            for _ in range(3):
                svc.decide_matches(['m'])
                svc.live_expert.flush_decisions()
            self.assertEqual(len(svc.ledger.load()), 4)
            restarted = DecisionLedger(root + '/ledger')
            e = row['evaluations'][0]
            self.assertEqual(restarted.record_live({**row, 'algorithm': e['algorithm'], 'forecast': e['forecasts'][0]}), 0)
            self.assertEqual(restarted.settle({'m': {'ft': [2, 1], 'done': False}})['settled'], 0)
            self.assertEqual(restarted.settle({'m': {'ft': [2, 1], 'done': True}})['settled'], 4)
            history = restarted.history(cohort='prospective')
            self.assertEqual(history['overall']['direction_samples'], 2)
            self.assertEqual(history['overall']['direction_accuracy'], 1)
            self.assertEqual(len(history['by_algorithm']), 2)
            for before, after in zip(original, restarted.load()):
                self.assertEqual((before.at, before.odds, before.entry_score), (after.at, after.odds, after.entry_score))
            self.assertTrue(all(e['correct'] is not None for e in history['entries']))
            self.assertEqual(len(restarted.history(algorithm='poisson_market')['entries']), 2)
            self.assertEqual(restarted.history(cohort='legacy')['entries'], [])

    def test_absent_historical_match_lookup_requires_confirmed_end(self):
        svc = AnalysisService(MagicMock(), config=AnalysisConfig(use_llm=False))
        svc.valuation.source.schedule.return_value=[]
        done = MagicMock(mid='old', score=(1, 1), is_finished=True)
        svc.valuation.source.odds.return_value=[done]
        self.assertEqual(svc._finished_matches({'old'}), [done])
        done.is_finished=False
        self.assertEqual(svc._finished_matches({'old'}), [])

    def test_config_change_while_calculating_discards_old_generation(self):
        hub = test_live_expert.LiveExpertTests().hub()
        svc = AnalysisService(MagicMock(), realtime=hub, config=AnalysisConfig(use_llm=False))
        actual = svc.live_expert._calculate
        def calculate(snapshot):
            out = actual(snapshot)
            svc.live_expert.config_version += 1
            return out
        with patch.object(svc.live_expert, '_calculate', side_effect=calculate):
            self.assertEqual(svc.live_expert.compute(hub.decision_snapshot('m'), hub), {})
        self.assertEqual(svc.live_expert.results()['count'], 0)

    def test_recommendations_have_their_own_frozen_identity_and_accuracy(self):
        with tempfile.TemporaryDirectory() as root:
            hub = test_live_expert.LiveExpertTests().hub()
            svc = AnalysisService(MagicMock(), realtime=hub, config=AnalysisConfig(use_llm=False, ledger_root=root + '/ledger'))
            row = svc.decide_matches(['m'])['decisions'][0]
            f = row['evaluations'][0]['forecasts'][0]
            forecast = {**row, 'algorithm': 'poisson_market', 'forecast': f}
            self.assertEqual(svc.ledger.record_live(forecast), 1)
            recommendation = {**forecast, 'record_kind': 'recommendation'}
            self.assertEqual(svc.ledger.record_live(recommendation), 1)
            self.assertEqual(svc.ledger.record_live({**recommendation, 'config_version': 99}), 0)
            svc.ledger.settle({'m': {'ft': [0, 1]}})
            history = svc.ledger.history(cohort='recommendations')
            self.assertEqual(history['overall']['direction_samples'], 1)
            self.assertEqual(history['overall']['direction_accuracy'], 0)
            self.assertFalse(history['entries'][0]['correct'])
