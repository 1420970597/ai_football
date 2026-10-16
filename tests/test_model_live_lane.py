"""Live shadow output must make progress while historical PG ingest waits."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import gzip
import fcntl
import json
import threading
import time
import unittest

from core.learned_model import FEATURES
from service.model_service import ModelService, journal_members, journal_tail_boundary
from service.model_training import atomic_json
from store.history import HistoryJournal
from tests import test_shadow_observation
from tests.test_training_data import decision


class ModelLiveLaneTests(unittest.TestCase):
    def setup_worker(self, directory):
        root = Path(directory)
        ledger = root/'ledger'
        ledger.mkdir()
        atomic_json(root/'models'/'candidate-v1.json',test_shadow_observation.ShadowObservationTests().artifact())
        worker = ModelService()
        worker.bind(str(ledger))
        worker.configure(replace(worker.cfg,model_training_enabled=False))
        return worker,ledger

    def row(self, **changes):
        row = decision()
        row.update(input_cutoff_ms=1735732800000., received_at_ms=1735732800000.,**changes)
        for evaluation in row['evaluations']:
            for candidate in evaluation['candidates']:
                candidate.update(ts_ms=1735732800000.,quote_received_at_ms=1735732800000.)
        return row

    def test_durable_producer_progresses_while_pg_waits_and_consumer_merges_batches(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-persist-decoupled-') as directory:
            worker,ledger = self.setup_worker(directory)
            worker._initialize_live_cursor()
            journal = HistoryJournal(ledger/'live-replay.jsonl.gz')
            journal.append([self.row()])
            worker._consume_live_shadow(enqueue_only=True)
            first_cursor = dict(worker._live_cursor)
            entered,release = threading.Event(),threading.Event()
            committed = []
            def write(db,path,rows):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test did not release PG')
                committed.extend(rows)
                return rows
            with patch('service.model_service.persist_prospective_predictions',side_effect=write):
                consumer = threading.Thread(target=worker._persist_live_outbox)
                consumer.start()
                self.assertTrue(entered.wait(1))
                try:
                    for minute in (1,2):
                        journal.append([self.row(computed_at=f'2025-01-01T12:0{minute}:00+00:00')])
                        worker._consume_live_shadow(enqueue_only=True)
                    self.assertNotEqual(worker._live_cursor,first_cursor)
                    self.assertEqual(len(list((Path(directory)/'models'/'live-shadow-outbox').glob('*.json'))),3)
                    self.assertTrue(consumer.is_alive())
                    self.assertFalse(worker._shadow)
                finally:
                    release.set()
                    consumer.join(2)
                worker._persist_live_outbox()
            self.assertEqual(len(committed),60)
            self.assertEqual(len({(r['at'],r['algorithm'],r['market'],r['line'],r['outcome']) for r in committed}),60)
            self.assertEqual(worker._live_stats['last_persist_batch_files'],2)
            self.assertEqual(worker._live_stats['outbox_batches'],0)

    def test_durable_queue_restart_retries_original_probabilities_without_cursor_rewind(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-persist-restart-') as directory:
            worker,ledger = self.setup_worker(directory)
            worker._initialize_live_cursor()
            journal = HistoryJournal(ledger/'live-replay.jsonl.gz')
            journal.append([self.row()])
            worker._consume_live_shadow(enqueue_only=True)
            queue = Path(directory)/'models'/'live-shadow-outbox'
            original = json.loads(next(queue.glob('*.json')).read_text())
            with patch('service.model_service.persist_prospective_predictions',side_effect=TimeoutError('PG')):
                with self.assertRaises(TimeoutError):
                    worker._persist_live_outbox()
            journal.append([self.row(computed_at='2025-01-01T12:01:00+00:00')])
            worker._consume_live_shadow(enqueue_only=True)
            saved_cursor = dict(worker._live_cursor)
            fresh = ModelService()
            fresh.bind(str(ledger))
            fresh._initialize_live_cursor()
            fresh._shadow_model['weights'] = [999.]*len(FEATURES)
            fresh._consume_live_shadow(enqueue_only=True)
            self.assertEqual(fresh._live_cursor,saved_cursor)
            with patch('service.model_service.persist_prospective_predictions',side_effect=lambda db,path,rows:rows) as writer:
                fresh._persist_live_outbox()
            self.assertEqual(writer.call_args.args[2][:len(original['predictions'])],original['predictions'])
            self.assertFalse(list(queue.glob('*.json')))
            self.assertFalse(fresh._shadow)

    def test_failed_historical_input_batch_is_not_repeated_in_memory(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-input-retry-') as directory:
            worker,ledger = self.setup_worker(directory)
            journal = HistoryJournal(ledger/'live-replay.jsonl.gz')
            journal.append([self.row()])
            with patch('service.model_service.write_inputs',side_effect=TimeoutError('PG')):
                for _ in range(3):
                    with self.assertRaises(TimeoutError):
                        worker._consume_replay()
                    self.assertEqual(len(worker._inputs),20)
                    self.assertFalse(worker._cursor)
            with patch('service.model_service.write_inputs') as writer:
                worker._consume_replay()
            self.assertEqual(writer.call_count,2)
            self.assertFalse(worker._inputs)
            self.assertEqual(worker._cursor[next(ledger.glob('live-replay*.jsonl.gz')).name],
                             next(ledger.glob('live-replay*.jsonl.gz')).stat().st_size)

    def test_queued_batch_restores_cursor_after_checkpoint_crash_and_repairs_inode(self):
        for repair in (False,True):
            with self.subTest(repair=repair), TemporaryDirectory(
                    dir=Path.cwd()/'output',prefix='lane-queued-checkpoint-') as directory:
                worker,ledger = self.setup_worker(directory)
                path = ledger/'live-replay.2025010112.jsonl.gz'
                path.write_bytes(b'')
                worker._initialize_live_cursor()
                path.write_bytes(gzip.compress((json.dumps(self.row())+'\n').encode()))
                original_checkpoint = json.loads((Path(directory)/'models'/'live-shadow-cursor.json').read_text())
                worker._consume_live_shadow(enqueue_only=True)
                queue = Path(directory)/'models'/'live-shadow-outbox'
                original_batch = json.loads(next(queue.glob('*.json')).read_text())
                atomic_json(Path(directory)/'models'/'live-shadow-cursor.json',original_checkpoint)
                if repair:
                    replacement = ledger/'replacement.jsonl.gz'
                    replacement.write_bytes(gzip.compress((json.dumps({**self.row(),'extra':'x'*5000})+'\n').encode()))
                    replacement.replace(path)
                fresh = ModelService()
                fresh.bind(str(ledger))
                fresh._initialize_live_cursor()
                fresh._shadow_model['weights'] = [999.]*len(FEATURES)
                fresh._consume_live_shadow(enqueue_only=True)
                self.assertEqual(fresh._live_cursor[path.name],path.stat().st_size)
                self.assertFalse(fresh._shadow)
                with patch('service.model_service.persist_prospective_predictions',side_effect=lambda db,path,rows:rows) as writer:
                    fresh._persist_live_outbox()
                self.assertEqual(writer.call_args.args[2],original_batch['predictions'])
                self.assertFalse(list(queue.glob('*.json')))

    def test_durable_queue_budget_stops_before_decode_without_deleting_predictions(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-outbox-budget-') as directory:
            worker,ledger = self.setup_worker(directory)
            worker._initialize_live_cursor()
            journal = HistoryJournal(ledger/'live-replay.jsonl.gz')
            journal.append([self.row()])
            worker._consume_live_shadow(enqueue_only=True)
            cursor = dict(worker._live_cursor)
            journal.append([self.row(computed_at='2025-01-01T12:01:00+00:00')])
            with patch('service.model_service.LIVE_OUTBOX_MAX_BYTES',1):
                with self.assertRaisesRegex(ValueError,'磁盘预算'):
                    worker._consume_live_shadow(enqueue_only=True)
            self.assertEqual(worker._live_cursor,cursor)
            self.assertEqual(worker._live_stats['outbox_predictions'],20)
            self.assertEqual(len(list((Path(directory)/'models'/'live-shadow-outbox').glob('*.json'))),1)

    def test_outbox_directory_sync_failure_does_not_acknowledge_journal(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-outbox-fsync-') as directory:
            worker,ledger = self.setup_worker(directory)
            worker._initialize_live_cursor()
            journal = HistoryJournal(ledger/'live-replay.jsonl.gz')
            journal.append([self.row()])
            cursor = dict(worker._live_cursor)
            queue = Path(directory)/'models'/'live-shadow-outbox'
            queue.mkdir()
            with patch('service.model_service.fsync_directory',side_effect=OSError('fsync failed')):
                with self.assertRaisesRegex(OSError,'fsync failed'):
                    worker._consume_live_shadow(enqueue_only=True)
            # Discovering a new hourly journal registers its initial zero
            # boundary, but failed durability must never acknowledge a member.
            self.assertTrue(all(offset == cursor.get(name, 0)
                                for name, offset in worker._live_cursor.items()))
            queued = json.loads(next(queue.glob('*.json')).read_text())
            self.assertEqual(len(queued['predictions']),20)
            fresh = ModelService()
            fresh.bind(str(ledger))
            fresh._initialize_live_cursor()
            fresh._consume_live_shadow(enqueue_only=True)
            self.assertEqual(fresh._live_cursor,queued['cursor'])
            self.assertFalse(fresh._shadow)

    def test_pg_settings_reload_is_versioned_and_failure_preserves_configuration(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-pg-settings-') as directory:
            worker,_ = self.setup_worker(directory)
            document = {'version':3,'settings':{'decision_mode':'hybrid','model_blend_weight':.6}}
            with patch('service.model_service.projection_target_from_env',return_value=object()), \
                    patch('service.model_service.load_runtime_settings',side_effect=[document,TimeoutError('PG'),document]) as load:
                worker._reload_settings()
                self.assertEqual(worker.cfg.decision_mode,'hybrid')
                self.assertEqual(worker.cfg.model_blend_weight,.6)
                worker._reload_settings()
                self.assertEqual(load.call_count,1)
                worker._next_settings_reload = 0
                worker._reload_settings()
                self.assertEqual(worker.cfg.decision_mode,'hybrid')
                self.assertEqual(worker._settings_version,3)
                self.assertIn('PG',worker._status['settings_error'])
                worker._next_settings_reload = 0
                worker._reload_settings()
                self.assertNotIn('settings_error',worker._status)

    def test_first_pg_settings_failure_or_missing_document_prevents_threads_and_training(self):
        for result in (TimeoutError('PG unavailable'),None):
            with self.subTest(result=result), TemporaryDirectory(
                    dir=Path.cwd()/'output',prefix='lane-start-settings-') as directory:
                worker,_ = self.setup_worker(directory)
                with patch('service.model_service.projection_target_from_env',return_value=object()), \
                        patch('service.model_service.load_runtime_settings',side_effect=[result]), \
                        patch('service.model_service.prepare_postgres_schema') as schema, \
                        patch.object(worker,'_initialize_live_cursor') as initialize:
                    with self.assertRaisesRegex(RuntimeError,'首次PG设置加载失败'):
                        worker.start()
                self.assertIsNone(worker._settings_version)
                self.assertIsNone(worker._thread)
                self.assertIsNone(worker._live_thread)
                self.assertIsNone(worker._persist_thread)
                self.assertIsNone(worker._process)
                self.assertTrue(worker._status['settings_error'])
                schema.assert_not_called()
                initialize.assert_not_called()

    def test_pg_start_applies_saved_settings_and_preserves_them_on_later_pg_failure(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-start-saved-') as directory:
            worker,_ = self.setup_worker(directory)
            saved = {'version':7,'settings':{'model_training_enabled':False,
                'model_shadow_enabled':False,'decision_mode':'hybrid','model_blend_weight':.7}}
            with patch('service.model_service.projection_target_from_env',return_value=object()), \
                    patch('service.model_service.load_runtime_settings',side_effect=[saved,TimeoutError('PG')]), \
                    patch('service.model_service.prepare_postgres_schema'), \
                    patch.object(worker,'_initialize_live_cursor'), \
                    patch('service.model_service.threading.Thread') as thread:
                worker.start()
                self.assertEqual(thread.call_count,4)
                self.assertFalse(worker.cfg.model_training_enabled)
                self.assertFalse(worker.cfg.model_shadow_enabled)
                self.assertEqual(worker.cfg.decision_mode,'hybrid')
                self.assertEqual(worker.cfg.model_blend_weight,.7)
                self.assertEqual(worker._settings_version,7)
                worker._thread = worker._live_thread = worker._persist_thread = worker._replay_thread = None
                worker._next_settings_reload = 0
                worker.start()
                self.assertEqual(thread.call_count,8)
                self.assertFalse(worker.cfg.model_training_enabled)
                self.assertFalse(worker.cfg.model_shadow_enabled)
                self.assertEqual(worker.cfg.decision_mode,'hybrid')
                self.assertEqual(worker._settings_version,7)
                self.assertIn('PG',worker._status['settings_error'])

    def test_initial_tail_skips_old_shadow_and_partial_member_retries(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-tail-') as directory:
            worker,ledger = self.setup_worker(directory)
            path = ledger/'live-replay.jsonl.gz'
            journal = HistoryJournal(path)
            journal.append([self.row()])
            path = next(ledger.glob('live-replay*.jsonl.gz'))
            boundary = path.stat().st_size
            partial = gzip.compress((json.dumps(self.row(computed_at='2025-01-01T12:01:00+00:00'))+'\n').encode())
            with path.open('ab') as source:
                source.write(partial[:10])
            self.assertEqual(journal_tail_boundary(path),boundary)
            worker._initialize_live_cursor()
            self.assertEqual(worker._live_cursor[path.name],boundary)
            worker._consume_live_shadow()
            self.assertEqual(worker._live_cursor[path.name],boundary)
            with path.open('ab') as source:
                source.write(partial[10:])
            with patch('service.model_service.time.time',return_value=1735732861):
                worker._consume_live_shadow()
            self.assertEqual(worker._live_cursor[path.name],path.stat().st_size)
            self.assertEqual(worker._shadow_info['predictions_written'],20)
            self.assertFalse(worker._inputs)
            self.assertEqual(worker._live_stats['skipped_existing_journals'],1)

    def test_historical_projection_does_not_generate_shadow_or_hold_live_output(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-priority-') as directory:
            worker,ledger = self.setup_worker(directory)
            path = ledger/'live-replay.jsonl.gz'
            journal = HistoryJournal(path)
            journal.append([self.row()])
            path = next(ledger.glob('live-replay*.jsonl.gz'))
            worker._initialize_live_cursor()
            entered,release = threading.Event(),threading.Event()
            def blocked_inputs(*args,**kwargs):
                entered.set()
                release.wait(5)
            with patch('service.model_service.write_inputs',side_effect=blocked_inputs), \
                    patch('service.model_service.write_predictions') as writer:
                replay = threading.Thread(target=worker._consume_replay)
                replay.start()
                self.assertTrue(entered.wait(1))
                self.assertFalse(worker._shadow)
                journal.append([self.row(computed_at='2025-01-01T12:01:00+00:00')])
                with patch('service.model_service.time.time',return_value=1735732861):
                    worker._consume_live_shadow()
                self.assertTrue(replay.is_alive())
                self.assertEqual(writer.call_count,1)
                self.assertEqual(worker._shadow_info['predictions_written'],20)
                self.assertEqual(worker.status()['shadow']['performance']['samples'],1)
                release.set()
                replay.join(2)
            self.assertFalse(replay.is_alive())
            # First historical cursor was committed before the new member;
            # another independent cycle permanently ingests that new input.
            worker._consume_replay()
            self.assertEqual(worker._cursor[path.name],path.stat().st_size)

    def test_outbox_preserves_original_probability_timestamp_across_retry_and_restart(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-outbox-') as directory:
            worker,ledger = self.setup_worker(directory)
            worker._initialize_live_cursor()
            journal = HistoryJournal(ledger/'live-replay.jsonl.gz')
            journal.append([self.row()])
            with patch('service.model_service.time.time',return_value=1735732801), \
                    patch('service.model_service.write_predictions',side_effect=TimeoutError('PG unavailable')):
                with self.assertRaises(TimeoutError):
                    worker._consume_live_shadow()
            outbox = Path(directory)/'models'/'live-shadow-outbox.json'
            original = json.loads(outbox.read_text())
            self.assertEqual(len(original['predictions']),20)
            fresh = ModelService()
            fresh.bind(str(ledger))
            fresh._initialize_live_cursor()
            fresh._shadow_model['weights'] = [999.]*len(FEATURES)
            with patch('service.model_service.write_predictions') as persisted:
                fresh._consume_live_shadow()
            self.assertEqual(persisted.call_args.args[1],original['predictions'])
            self.assertFalse(outbox.exists())
            self.assertEqual(fresh._live_cursor,original['cursor'])
            self.assertEqual(fresh.status()['shadow']['performance']['samples'],0)
            self.assertEqual(fresh._live_stats['recovered_outbox_predictions'],20)

    def test_latency_has_real_compute_queue_generation_and_commit_segments(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-metrics-') as directory:
            worker,ledger = self.setup_worker(directory)
            worker._initialize_live_cursor()
            with patch('service.model_service.time.time',side_effect=[1735732800.5,1735732800.7]), \
                    patch('service.model_service.time.perf_counter',side_effect=[1.,1.05]):
                worker.observe(self.row(),record_inputs=False,realtime=True)
            with patch('service.model_service.time.perf_counter',side_effect=[2.,2.08]):
                worker._flush_predictions(realtime=True)
            performance = worker.status()['shadow']['performance']
            self.assertAlmostEqual(performance['compute_ms']['p95'],50)
            self.assertAlmostEqual(performance['queue_ms']['p95'],500)
            self.assertAlmostEqual(performance['decision_to_prediction_ms']['p95'],700,places=2)
            self.assertAlmostEqual(performance['prediction_persist_ms']['p95'],80)
            self.assertEqual(performance['scope'],'live_lane_recent_process_samples')

    def test_live_error_is_rate_limited_and_threads_stop_cleanly(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-stop-') as directory:
            worker,_ = self.setup_worker(directory)
            with patch('service.model_service.projection_target_from_env',return_value=None), \
                    patch.object(worker,'_loop',side_effect=lambda:worker._stop.wait(5)), \
                    patch.object(worker,'_consume_live_shadow',side_effect=TimeoutError('retryable')) as consume, \
                    patch.object(worker,'_publish'):
                worker.start()
                deadline = time.monotonic()+1
                while consume.call_count == 0 and time.monotonic()<deadline:
                    time.sleep(.005)
                worker.stop()
            self.assertEqual(consume.call_count,1)
            self.assertIsNone(worker._thread)
            self.assertIsNone(worker._live_thread)
            self.assertEqual(worker._live_stats['state'],'error')

    def test_shared_parser_budget_preserves_complete_cursor(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-parser-') as directory:
            path = Path(directory)/'live-replay.jsonl.gz'
            HistoryJournal(path).append([self.row()])
            path = next(Path(directory).glob('live-replay*.jsonl.gz'))
            with patch('service.model_service.time.monotonic',side_effect=[0.,2.]):
                self.assertEqual(list(journal_members(path,0,limit=16,budget_s=.1)),[])
            parsed = list(journal_members(path,0,limit=1,budget_s=1))
            self.assertEqual(parsed[0][0],path.stat().st_size)
            self.assertEqual(parsed[0][1][0]['match_id'],'m1')

    def test_rollover_prioritizes_new_journal_and_fairly_drains_older_suffix(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-rollover-') as directory:
            worker,ledger = self.setup_worker(directory)
            older = ledger/'live-replay.2025010111.jsonl.gz'
            older.write_bytes(gzip.compress((json.dumps(self.row())+'\n').encode()))
            worker._initialize_live_cursor()
            with older.open('ab') as source:
                source.write(gzip.compress((json.dumps(self.row(computed_at='2025-01-01T12:01:00+00:00'))+'\n').encode()))
            newest = ledger/'live-replay.2025010112.jsonl.gz'
            newest.write_bytes(gzip.compress((json.dumps(self.row(computed_at='2025-01-01T12:02:00+00:00'))+'\n').encode()))
            with patch('service.model_service.time.time',return_value=1735732921):
                worker._consume_live_shadow()
                self.assertEqual(worker._live_stats['last_file'],newest.name)
                worker._consume_live_shadow()
                self.assertEqual(worker._live_stats['last_file'],older.name)
            self.assertEqual(worker._live_cursor[older.name],older.stat().st_size)
            self.assertEqual(worker._live_cursor[newest.name],newest.stat().st_size)

    def test_recovery_scheduler_gives_four_newest_batches_then_an_old_batch(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-recovery-schedule-') as directory:
            worker,ledger = self.setup_worker(directory)
            older = ledger/'live-replay.2025010111.jsonl.gz'
            newest = ledger/'live-replay.2025010112.jsonl.gz'
            older.write_bytes(b'x'*100)
            newest.write_bytes(b'x'*100)
            worker._live_cursor = {older.name:0,newest.name:0}
            worker._live_inodes = {p.name:[p.stat().st_dev,p.stat().st_ino] for p in (older,newest)}
            visits=[]
            def parser(path,offset,*,limit,budget_s):
                visits.append(path.name)
                self.assertEqual(limit,64)
                self.assertEqual(budget_s,.25)
                yield offset+1,[]
            with patch('service.model_service.journal_members',side_effect=parser):
                for _ in range(10):
                    cursor,_,_,_ = worker._read_live_batch_locked()
                    worker._live_cursor.update(cursor)
            self.assertEqual(visits,[newest.name]*4+[older.name]+[newest.name]*4+[older.name])
            self.assertTrue(worker._live_stats['catching_up'])

    def test_live_loop_removes_idle_wait_only_when_recovery_backlog_exists(self):
        for backlog in (False,True):
            with self.subTest(backlog=backlog):
                worker=ModelService()
                worker._live_stats['catching_up']=backlog
                waits=[]
                def wait(delay,waits=waits,worker=worker):
                    waits.append(delay)
                    worker._stop.set()
                with patch.object(worker,'_consume_live_shadow'),patch.object(worker,'_publish'), \
                        patch.object(worker._stop,'wait',side_effect=wait):
                    worker._live_loop()
                self.assertEqual(waits,[0 if backlog else .05])

    def test_oversized_member_is_rejected_before_unbounded_allocation_and_partial_cursor_preserved(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-size-') as directory:
            path = Path(directory)/'live-replay.jsonl.gz'
            path.write_bytes(gzip.compress(b'x'*4096))
            with patch('service.model_service.GZIP_MEMBER_MAX_BYTES',1024):
                with self.assertRaisesRegex(ValueError,'内存上限'):
                    list(journal_members(path,0,limit=1,budget_s=1))
                with self.assertRaisesRegex(ValueError,'内存上限'):
                    journal_tail_boundary(path)

    def test_restart_registers_multiple_new_hours_and_rotates_all_pending_suffixes(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-hour-restart-') as directory:
            worker,ledger = self.setup_worker(directory)
            oldest = ledger/'live-replay.2025010110.jsonl.gz'
            oldest.write_bytes(gzip.compress((json.dumps(self.row())+'\n').encode()))
            worker._initialize_live_cursor()
            known_boundary = oldest.stat().st_size
            with oldest.open('ab') as source:
                source.write(gzip.compress((json.dumps(self.row(computed_at='2025-01-01T12:01:00+00:00'))+'\n').encode()))
            middle = ledger/'live-replay.2025010111.jsonl.gz'
            newest = ledger/'live-replay.2025010112.jsonl.gz'
            for path,at in ((middle,'2025-01-01T12:02:00+00:00'),(newest,'2025-01-01T12:03:00+00:00')):
                path.write_bytes(gzip.compress((json.dumps(self.row(computed_at=at))+'\n').encode()))
            fresh = ModelService()
            fresh.bind(str(ledger))
            fresh._initialize_live_cursor()
            self.assertEqual(fresh._live_cursor[middle.name],0)
            self.assertEqual(fresh._live_cursor[newest.name],0)
            self.assertEqual(fresh._live_cursor[oldest.name],known_boundary)
            for _ in range(5):
                fresh._consume_live_shadow()
            self.assertEqual(fresh._live_cursor,{path.name:path.stat().st_size for path in (oldest,middle,newest)})
            self.assertEqual(fresh._live_stats['recovered_records'],3)
            self.assertEqual(fresh.status()['shadow']['performance']['samples'],0)

    def test_legacy_plain_cursor_does_not_make_new_hour_look_historical(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-legacy-') as directory:
            worker,ledger = self.setup_worker(directory)
            legacy = ledger/'live-replay.jsonl.gz'
            legacy.write_bytes(gzip.compress((json.dumps(self.row())+'\n').encode()))
            worker._initialize_live_cursor()
            new_hour = ledger/'live-replay.2025010112.jsonl.gz'
            new_hour.write_bytes(gzip.compress((json.dumps(self.row())+'\n').encode()))
            restored = ModelService()
            restored.bind(str(ledger))
            restored._initialize_live_cursor()
            self.assertEqual(restored._live_cursor[new_hour.name],0)

    def test_maintenance_inode_replacement_resets_safe_tail_without_poisoning_live_cursor(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-repair-') as directory:
            worker,ledger = self.setup_worker(directory)
            path = ledger/'live-replay.2025010112.jsonl.gz'
            path.write_bytes(gzip.compress((json.dumps(self.row())+'\n').encode()))
            worker._initialize_live_cursor()
            replacement = ledger/'replacement.jsonl.gz'
            replacement.write_bytes(gzip.compress((json.dumps(self.row(computed_at='2025-01-01T12:01:00+00:00'))+'\n').encode()))
            with (Path(directory)/'models'/'replay.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                replacement.replace(path)
            worker._consume_live_shadow()
            self.assertEqual(worker._live_cursor[path.name],path.stat().st_size)
            self.assertEqual(worker._live_stats['repair_tail_resets'],1)
            self.assertFalse(worker._shadow)
            with path.open('ab') as source:
                source.write(gzip.compress((json.dumps(self.row(computed_at='2025-01-01T12:02:00+00:00'))+'\n').encode()))
            def writer(*args):
                # A slow/failed PG transaction must not retain the repair SH
                # lock, otherwise maintenance would block behind network IO.
                with (Path(directory)/'models'/'replay.lock').open('a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with patch('service.model_service.write_predictions',side_effect=writer):
                worker._consume_live_shadow()
            self.assertEqual(worker._shadow_info['predictions_written'],20)
            self.assertEqual(worker._live_cursor[path.name],path.stat().st_size)

    def test_failed_pg_outbox_then_maintenance_then_restart_cannot_restore_old_inode_offset(self):
        with TemporaryDirectory(dir=Path.cwd()/'output',prefix='lane-repair-outbox-') as directory:
            worker,ledger = self.setup_worker(directory)
            path = ledger/'live-replay.2025010112.jsonl.gz'
            path.write_bytes(b'')
            worker._initialize_live_cursor()
            path.write_bytes(gzip.compress((json.dumps(self.row())+'\n').encode()))
            with patch('service.model_service.write_predictions',side_effect=TimeoutError('PG unavailable')):
                with self.assertRaises(TimeoutError):
                    worker._consume_live_shadow()
            outbox = Path(directory)/'models'/'live-shadow-outbox.json'
            original = json.loads(outbox.read_text())
            replacement = ledger/'replacement.jsonl.gz'
            replacement.write_bytes(gzip.compress((json.dumps({**self.row(),'repair_extra':'x'*5000})+'\n').encode()))
            with (Path(directory)/'models'/'replay.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                replacement.replace(path)
            fresh = ModelService()
            fresh.bind(str(ledger))
            fresh._initialize_live_cursor()
            with patch('service.model_service.write_predictions') as persisted:
                fresh._consume_live_shadow()
            self.assertEqual(persisted.call_args.args[1],original['predictions'])
            self.assertEqual(fresh._live_cursor[path.name],path.stat().st_size)
            self.assertNotEqual(fresh._live_cursor[path.name],original['cursor'][path.name])
            self.assertEqual(fresh._live_stats['outbox_cursor_inode_mismatches'],1)
            with path.open('ab') as source:
                source.write(gzip.compress((json.dumps(self.row(computed_at='2025-01-01T12:02:00+00:00'))+'\n').encode()))
            fresh._consume_live_shadow()
            self.assertEqual(fresh._live_cursor[path.name],path.stat().st_size)
