import gzip
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from service.live_expert import LiveExpertService
from service.live_workers import LiveCalculationPool, ReplayProcessWriter, LiveWorkerError
from service.runtime_settings import RuntimeConfig
from tests import test_live_expert


class LiveWorkersTests(unittest.TestCase):
    def test_scheduler_waits_when_all_pending_matches_belong_to_busy_shard(self):
        import time
        from unittest.mock import MagicMock
        from service.analysis import AnalysisService, AnalysisConfig
        hub = test_live_expert.LiveExpertTests().hub()
        service = AnalysisService(MagicMock(), realtime=hub, config=AnalysisConfig(use_llm=False))
        pool = MagicMock()
        pool.slots = [1, 2]
        pool.shard.side_effect = lambda mid: int(mid)%2
        service.live_expert.calculation_pool = pool
        service._pending = {'0': time.time()-10}
        service._live_inflight = {0}
        self.assertIsNone(service._next_trigger_wait())
        service._pending['1'] = time.time()+.1
        self.assertGreater(service._next_trigger_wait(), 0)
        service._live_inflight.clear()
        self.assertEqual(service._next_trigger_wait(), 0)

    def test_isolated_algorithm_retains_match_state_and_config_version(self):
        pool = LiveCalculationPool(2)
        self.addCleanup(pool.close)
        pool.start()
        hub = test_live_expert.LiveExpertTests().hub()
        service = LiveExpertService()
        config = replace(RuntimeConfig(), llm_api_key='never-cross-ipc')
        first, anchor, histories = pool.calculate(hub.decision_snapshot('m'), config, 2, service._performance)
        second, next_anchor, _ = pool.calculate(hub.decision_snapshot('m'), config, 2, service._performance)
        self.assertEqual(first['config_version'], 2)
        self.assertEqual(anchor, next_anchor)
        self.assertTrue(histories)
        self.assertEqual(first['probabilities'], second['probabilities'])
        self.assertEqual(pool.shard('m'), pool.shard('m'))
        self.assertEqual(pool.health()['ready'], 2)

    def test_fast_scheduler_advances_free_shard_without_batch_barrier(self):
        import time
        from unittest.mock import MagicMock
        from concurrent.futures import ThreadPoolExecutor
        from service.analysis import AnalysisService, AnalysisConfig
        hub = test_live_expert.LiveExpertTests().hub()
        service = AnalysisService(MagicMock(), realtime=hub, config=AnalysisConfig(use_llm=False))
        service.live_expert.calculation_pool = MagicMock()
        service.live_expert.calculation_pool.slots = [1, 2]
        service.live_expert.calculation_pool.shard.side_effect = lambda mid: int(mid)%2
        service._live_threads = ThreadPoolExecutor(max_workers=2)
        entered, release, free_done = threading.Event(), threading.Event(), threading.Event()
        def compute(mid):
            if mid == '0':
                entered.set()
                release.wait(2)
            else:
                free_done.set()
            return {}
        service._compute_live_match = compute
        try:
            service._pending = {'0': time.time()-1, '1': time.time()-1}
            service._trigger_batch()
            self.assertTrue(entered.wait(1))
            self.assertTrue(free_done.wait(1))
            until = time.monotonic()+1
            while 1 in service._live_inflight and time.monotonic()<until:
                time.sleep(.001)
            free_done.clear()
            service._pending['3'] = time.time()-1
            service._trigger_batch()
            self.assertTrue(free_done.wait(1))
            self.assertIn(0, service._live_inflight)
        finally:
            release.set()
            service._live_threads.shutdown(wait=True)
            service.live_expert.calculation_pool = None

    def test_failed_worker_does_not_publish_and_recovers(self):
        pool = LiveCalculationPool(1)
        self.addCleanup(pool.close)
        pool.start()
        service = LiveExpertService()
        with self.assertRaises(LiveWorkerError):
            pool.calculate({'match_id': 'm', 'quotes': ['invalid']}, service.config, 1, service._performance)
        hub = test_live_expert.LiveExpertTests().hub()
        result, _, _ = pool.calculate(hub.decision_snapshot('m'), service.config, 1, service._performance)
        self.assertEqual(result['match_id'], 'm')

    def test_replay_process_fsync_ack_and_complete_record(self):
        with tempfile.TemporaryDirectory() as root:
            writer = ReplayProcessWriter(Path(root)/'records.gz')
            try:
                self.assertEqual(writer.append([{'publication_id': 'one', 'markets': [1, 2]}]), 1)
                with gzip.open(writer.path, 'rt') as source:
                    self.assertEqual(json.loads(source.read())['markets'], [1, 2])
            finally:
                writer.close()

    def test_worker_frozen_replay_preserves_input_and_output_before_public_mutation(self):
        pool = LiveCalculationPool(1)
        self.addCleanup(pool.close)
        pool.start()
        hub = test_live_expert.LiveExpertTests().hub()
        service = LiveExpertService()
        snapshot = hub.decision_snapshot('m')
        config = replace(service.config, llm_api_key='fixture-not-in-replay')
        result, _, _, frozen = pool.calculate(snapshot, config, 3, service._performance, replay=True)
        expected = result['evaluations'][0]['candidates'][0]['p_model']
        result['evaluations'][0]['candidates'][0]['p_model'] = -1
        snapshot['status']['mst'] = '0'
        frozen.metadata.update(publication_id='final-id', input_cutoff_ms=snapshot['captured_at_ms'])
        with tempfile.TemporaryDirectory() as root:
            writer = ReplayProcessWriter(Path(root)/'records.gz')
            try:
                self.assertEqual(writer.append([frozen]), 1)
                with gzip.open(writer.path, 'rt') as source:
                    row = json.loads(source.read())
                self.assertEqual(row['publication_id'], 'final-id')
                self.assertEqual(row['config_version'], 3)
                self.assertEqual(row['input_state']['status']['mst'], '3600')
                self.assertEqual(row['evaluations'][0]['candidates'][0]['p_model'], expected)
                self.assertNotIn('llm_api_key', row['config'])
                self.assertNotIn('fixture-not-in-replay', json.dumps(row))
                self.assertTrue(row['markets'])
            finally:
                writer.close()

    def test_compact_dispatch_keeps_full_raw_markets_in_detail_and_replay(self):
        from collector.leyu_realtime import PriceTick
        import time
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'LIVE_COMPUTE_PROCESSES': '1'}):
            service = LiveExpertService()
            service.start(root)
            try:
                hub = test_live_expert.LiveExpertTests().hub()
                hub._record_ticks([PriceTick('m', '998', '998', '', 'yes', 'yes', 1.9, 1.9,
                                             int(time.time()*1000))])
                row = service.compute(hub.decision_snapshot('m'), hub)
                self.assertFalse(any(m['market'].startswith('RAW_') for m in row['markets']))
                self.assertNotIn('candidates', row)
                self.assertTrue(row['evaluations'][0]['forecasts'])
                self.assertNotIn('candidates', row['evaluations'][0])
                detail = service.detail('m')
                self.assertTrue(any(m['market']=='RAW_998' for m in detail['markets']))
                self.assertTrue(detail['candidates'])
                self.assertTrue(detail['evaluations'][0]['candidates'])
                self.assertTrue(detail['ensemble']['candidates'])
                self.assertIn('RAW_998||yes', detail['price_history'])
                # Mutating a detail cannot corrupt the frozen training record.
                detail['markets'].clear()
                self.assertTrue(service.wait_replay(row, 1))
                with gzip.open(Path(root)/'live-replay.jsonl.gz', 'rt') as source:
                    replay = json.loads(source.read())
                self.assertTrue(any(m['market']=='RAW_998' for m in replay['markets']))
                self.assertEqual(len(detail['evaluations']), len(replay['evaluations']))
                self.assertNotIn('price_history', replay)
                self.assertEqual(replay['market_coverage']['total'], row['market_coverage']['total'])
                with self.assertRaises(ValueError):
                    service.calculation_pool.calculate(hub.decision_snapshot('m'), service.config, 1,
                                                       service._performance, compact=True)
            finally:
                service.stop()

    def test_persistence_ack_does_not_wait_for_slow_ledger_projection(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'LIVE_COMPUTE_PROCESSES': '1'}):
            service = LiveExpertService()
            entered, release = threading.Event(), threading.Event()
            def record(rows):
                entered.set()
                release.wait(3)
                return len(rows)
            service.on_decision = lambda row: 1
            service.on_decisions = record
            service.start(root)
            try:
                hub = test_live_expert.LiveExpertTests().hub()
                row = service.compute(hub.decision_snapshot('m'), hub)
                self.assertTrue(service.wait_replay(row, 1))
                self.assertTrue(entered.wait(1))
                self.assertEqual(service.health()['journal_pending'], 0)
            finally:
                release.set()
                service.stop()

    def test_stale_worker_config_cannot_repopulate_parent_anchor(self):
        service = LiveExpertService()
        hub = test_live_expert.LiveExpertTests().hub()
        from unittest.mock import MagicMock
        pool = MagicMock()
        def calculate(snapshot, config, version, evidence, **options):
            result = LiveExpertService()._calculate(snapshot)
            service.configure(replace(config, min_ev=.4), version+1)
            return result, {'stale': True}, {}
        pool.calculate.side_effect = calculate
        service.calculation_pool = pool
        self.assertEqual(service.compute(hub.decision_snapshot('m'), hub), {})
        self.assertEqual(service._anchors, {})
