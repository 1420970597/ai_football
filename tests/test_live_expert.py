import tempfile
import time
import unittest
import json
from unittest.mock import MagicMock, patch

from collector.leyu_realtime import PriceTick, RealtimeHub
from service.analysis import AnalysisConfig, AnalysisService
from service.live_expert import LiveExpertService, percentiles


class LiveExpertTests(unittest.TestCase):
    def hub(self):
        hub = RealtimeHub(MagicMock(), resume=False)
        now = time.monotonic()
        hub._info['m'] = {'match_id': 'm', 'league': '真实足球联赛', 'home': '甲', 'away': '乙', 'sport_id': '1'}
        hub._scores['m'] = (1, 0)
        hub._score_at['m'] = now
        hub._status_at['m'] = now
        hub._status['m'] = {'mmp': '7', 'mst': '3600'}
        self.ticks(hub)
        return hub

    def ticks(self, hub, over=1.9):
        ts = int(time.time() * 1000)
        ticks = [PriceTick('m', chpid, chpid, line, oc, oc, odds, odds, ts)
                 for chpid, line, quotes in [('2', '2.25', [('Over', over), ('Under', 1.98)]),
                                             ('1', '', [('1', 1.4), ('X', 4.0), ('2', 8.0)])]
                 for oc, odds in quotes]
        hub._record_ticks(ticks)

    def test_memory_compute_without_disk_network_or_llm(self):
        hub = self.hub()
        svc = AnalysisService(MagicMock(), realtime=hub, config=AnalysisConfig(use_llm=False))
        with patch.object(svc, 'refresh_matches', side_effect=AssertionError('REST hot path')):
            result = svc.decide_matches(['m'])
        row = result['decisions'][0]
        self.assertTrue(row['probabilities'])
        self.assertTrue(row['candidates'])
        self.assertEqual(row['decision'], 'observe')
        self.assertFalse(row['has_buy'])
        self.assertFalse(row['llm_used'])
        self.assertEqual(row['clock'], '60:00')

    def test_continuous_ticks_keep_first_due(self):
        svc = AnalysisService(MagicMock(), realtime=self.hub(), config=AnalysisConfig(use_llm=False))
        svc.notify_price_change(['m'])
        due = svc._pending['m']
        for _ in range(100):
            svc.notify_price_change(['m'])
        self.assertEqual(svc._pending['m'], due)
        self.assertEqual(svc.scheduler_health()['stats']['coalesced'], 100)

    def test_stale_missing_and_virtual_states_never_recommend(self):
        hub = self.hub()
        service = LiveExpertService()
        for change, expected in [({'score': None}, '缺少即时比分'),
                                 ({'suspended': True}, '盘口暂停'),
                                 ({'score_age_s': 500}, '比分数据过期'),
                                 ({'info': {'league': 'EAFC'}}, '虚拟比赛')]:
            snapshot = {**hub.decision_snapshot('m'), **change}
            row = service.compute(snapshot, hub)
            self.assertFalse(row['picks'])
            self.assertTrue(any(expected in r for r in row['reasons']))
            self.assertIsNone(row['remaining_goals'])

    def test_version_changed_cannot_publish_mixed_state(self):
        hub = self.hub()
        service = LiveExpertService()
        snapshot = hub.decision_snapshot('m')
        self.ticks(hub, 2.0)
        self.assertEqual(service.compute(snapshot, hub), {})
        self.assertEqual(service.health()['superseded'], 1)

    def test_all_state_events_notify_and_finish(self):
        hub = self.hub()
        callback = MagicMock()
        hub.on_state_change = callback
        for message in [dict(cmd='C102', cd={'mid': 'm', 'mst': '3660', 'mmp': '7'}),
                        dict(cmd='C110', cd={'mid': 'm', 'mc': 99999}),
                        dict(cmd='C303', cd={'mid': 'm', 'hpid': '2'}),
                        dict(cmd='C109', cd=[{'mid': 'm', 'ms': 110}])]:
            hub._handle_message(message)
        self.assertEqual(callback.call_count, 4)
        self.assertTrue(hub.decision_snapshot('m')['finished'])
        self.assertTrue(hub.decision_snapshot('m')['suspended'])
        self.assertTrue(hub.events('m')[0]['received_at'])

    def test_no_future_scores_used_and_async_journal(self):
        hub = self.hub()
        service = LiveExpertService()
        with tempfile.TemporaryDirectory() as root:
            service.journal_root = root
            row = service.compute(hub.decision_snapshot('m'), hub)
            self.assertEqual(row['score'], [1, 0])
            self.assertEqual(service.flush(), 1)
            from pathlib import Path
            record = json.loads((Path(root) / 'live-replay.jsonl').read_text())
            self.assertEqual(record['input_state']['status']['mst'], '3600')
            self.assertTrue(record['markets'])
            self.assertTrue(record['anchor'])
            self.assertEqual(record['remaining_goals'], row['remaining_goals'])
            self.assertEqual(service.flush(), 0)
            self.assertEqual(service.health()['journal_dropped'], 0)

    def test_percentiles_empty_and_outlier(self):
        self.assertIsNone(percentiles([])['p95'])
        self.assertEqual(percentiles([1] * 99 + [100])['p95'], 1)

    def test_workbench_api_filters_and_missing_detail(self):
        from api.app import ApiApp
        hub = self.hub()
        svc = AnalysisService(MagicMock(), realtime=hub, config=AnalysisConfig(use_llm=False))
        svc.decide_matches(['m'])
        app = ApiApp(MagicMock())
        app._analysis = svc
        svc.live_match_ids = lambda: {'m', 'other'}
        hub._subscribed = ['m']
        code, response = app.dispatch('GET', '/api/v1/workbench', {}, {})
        self.assertEqual(code, 200)
        self.assertEqual(response['count'], 1)
        self.assertTrue(response['matches'][0]['stale'])
        self.assertEqual(response['coverage']['source_current'], 2)
        self.assertEqual(response['coverage']['subscribed'], 1)
        self.assertEqual(response['coverage']['analyzed'], 1)
        with patch.object(hub.live, 'health', side_effect=AssertionError('full book scan')):
            code, _ = app.dispatch('GET', '/api/v1/workbench', {}, {})
            self.assertEqual(code, 200)
        code, _ = app.dispatch('GET', '/api/v1/workbench/nope', {}, {})
        self.assertEqual(code, 404)
        code, _ = app.dispatch('GET', '/api/v1/workbench', {'type': ['bogus']}, {})
        self.assertEqual(code, 400)


class HistoryTypeIsolationTests(unittest.TestCase):
    def test_api_keeps_real_and_virtual_history_separate(self):
        from api.app import ApiApp
        from service.ledger import LedgerEntry
        with tempfile.TemporaryDirectory() as root:
            svc = AnalysisService(MagicMock(), config=AnalysisConfig(use_llm=False, ledger_root=root))
            svc.ledger._append([LedgerEntry(at='2026-01-01', match_id='real', league='英超', is_pick=True),
                                LedgerEntry(at='2026-01-01', match_id='virtual', league='EAFC', is_pick=True)])
            app = ApiApp(MagicMock())
            app._analysis = svc
            for kind, mid in [('real', 'real'), ('virtual', 'virtual')]:
                code, result = app.dispatch('GET', '/api/v1/ledger/history', {'type': [kind]}, {})
                self.assertEqual(code, 200)
                self.assertEqual([e['match_id'] for e in result['entries']], [mid])


class UnknownStateMessageTests(unittest.TestCase):
    def test_unknown_market_state_is_not_invented_as_match_suspension(self):
        hub = RealtimeHub(MagicMock(), resume=False)
        callback = MagicMock()
        hub.on_state_change = callback
        hub._handle_message({'cmd': 'C104', 'cd': {'mid': 'm', 'hs': 0}})
        self.assertFalse(hub.decision_snapshot('m')['suspended'])
        callback.assert_called_once_with(['m'])

    def test_end_phase_hidden_without_inventing_settlement_proof(self):
        hub = RealtimeHub(MagicMock(), resume=False)
        hub._status['m'] = {'mmp': '999'}
        hub._changed_state('m')
        service = LiveExpertService()
        row = service.compute(hub.decision_snapshot('m'), hub)
        self.assertTrue(row['finished'])
        self.assertEqual(service.results()['count'], 0)
        self.assertFalse(hub.is_finished('m'))


class CurrentBookRetentionTests(unittest.TestCase):
    def test_load_ignores_expired_or_nonfinite_quotes(self):
        from pathlib import Path
        from collector.leyu_realtime import LiveBook, DEFAULT_QUOTE_MAX_AGE_S
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'live.json'
            now = int(time.time() * 1000)
            old = now - int((DEFAULT_QUOTE_MAX_AGE_S + 1) * 1000)
            path.write_text(json.dumps({'rows': [
                ['m', '2', '', 'old', 'Over', 2, old],
                ['m', '2', '', 'current', 'Over', 2, now],
                ['m', '2', '', 'invalid', 'Over', float('inf'), now]]}))
            book = LiveBook(str(path))
            self.assertEqual(book.load(), 1)
            self.assertEqual([q.oid for q in book.book('m')], ['current'])

    def test_save_prunes_old_options_and_preserves_current_match_index(self):
        from pathlib import Path
        from collector.leyu_realtime import LiveBook, DEFAULT_QUOTE_MAX_AGE_S
        with tempfile.TemporaryDirectory() as root:
            book = LiveBook(str(Path(root) / 'live.json'))
            now = int(time.time() * 1000)
            old = now - int((DEFAULT_QUOTE_MAX_AGE_S + 1) * 1000)
            book.upsert_many([PriceTick(mid, '2', '', '', oid, 'Over', 2, 2, ts)
                              for mid, oid, ts in [('m', 'old', old), ('m', 'current', now),
                                                   ('gone', 'old', old)]])
            self.assertTrue(book.save(force=True))
            self.assertEqual([q.oid for q in book.book('m')], ['current'])
            self.assertEqual(book.live_mids(), ['m'])
            restored = LiveBook(str(Path(root) / 'live.json'))
            self.assertEqual(restored.load(), 1)
            self.assertEqual([q.oid for q in restored.book('m')], ['current'])

    def test_no_persistence_does_not_discard_quotes(self):
        from collector.leyu_realtime import LiveBook
        book = LiveBook()
        book.upsert_many([PriceTick('m', '2', '', '', 'old', 'Over', 2, 2, 1)])
        self.assertFalse(book.save())
        self.assertEqual(len(book.book('m')), 1)
