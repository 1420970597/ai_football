from pathlib import Path
from dataclasses import replace
import gzip
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from service.model_service import ModelService
from service.runtime_settings import RuntimeConfig
from store.history import HistoryJournal
from tests.test_training_data import decision
from tests.test_model_validation import approved_artifact


class ModelServiceTests(unittest.TestCase):
    def test_input_replay_commits_while_evaluator_is_blocked_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory)/'ledger'
            ledger.mkdir()
            HistoryJournal(ledger/'live-replay.jsonl.gz').append([decision()])
            worker = ModelService()
            worker.bind(str(ledger))
            worker._stop.clear()
            evaluating, release, committed = threading.Event(), threading.Event(), threading.Event()

            def blocked_evaluation():
                evaluating.set()
                release.wait(3)

            from store.training_data import write_inputs as persist
            def persisted(*args):
                persist(*args)
                committed.set()

            with patch.object(worker, '_reload_settings'), patch.object(worker, '_read'), \
                    patch.object(worker, '_evaluate_shadow', side_effect=blocked_evaluation), \
                    patch.object(worker, '_publish'), patch.object(worker, '_start_training_check'), \
                    patch('service.model_service.subprocess.Popen'), \
                    patch('service.model_service.write_inputs', side_effect=persisted):
                worker._thread = threading.Thread(target=worker._loop)
                worker._replay_thread = threading.Thread(target=worker._replay_loop)
                coordinator, replay = worker._thread, worker._replay_thread
                coordinator.start()
                self.assertTrue(evaluating.wait(2))
                replay.start()
                try:
                    self.assertTrue(committed.wait(2), 'blocked evaluation must not block input commit')
                    self.assertTrue(coordinator.is_alive())
                    deadline = time.monotonic()+2
                    while not worker._cursor and time.monotonic()<deadline:
                        time.sleep(.01)
                    self.assertTrue(worker._cursor)
                finally:
                    release.set()
                    worker.stop()
                self.assertFalse(coordinator.is_alive())
                self.assertFalse(replay.is_alive())
                self.assertIsNone(worker._replay_thread)
            with sqlite3.connect(Path(directory)/'models'/'inputs.sqlite3') as con:
                self.assertEqual(con.execute('SELECT count(*) FROM inputs').fetchone()[0], 20)

    def test_growing_newest_journal_does_not_starve_older_qualified_inputs(self):
        import os
        from service.model_service import REPLAY_NEWEST_BATCHES
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory)/'ledger'
            ledger.mkdir()
            old = ledger/'live-replay.2026101000.jsonl.gz'
            recent = ledger/'live-replay.2026101100.jsonl.gz'
            HistoryJournal(old).append([decision()])
            worker = ModelService()
            worker.bind(str(ledger))
            for _index in range(REPLAY_NEWEST_BATCHES+1):
                HistoryJournal(recent).append([{**decision(), 'competition_type':'virtual'}])
                os.utime(old,ns=(1_000_000_000,1_000_000_000))
                worker._consume_replay()
            self.assertEqual(worker._cursor[old.name],old.stat().st_size)
            self.assertEqual(worker._replay_stats['total_inputs'],20)
            self.assertGreater(worker._replay_stats['backlog_bytes'],0)
            self.assertEqual(worker._replay_stats['counts_scope'],'current_process_committed_idempotent_rows')

    def test_postgres_projection_failure_keeps_inputs_and_predictions_for_retry(self):
        class PostgresFailure(Exception):
            pass
        with tempfile.TemporaryDirectory(dir=Path.cwd() / 'output', prefix='model-projection-') as root:
            worker = ModelService()
            worker.bind(str(Path(root)/'ledger'))
            worker._inputs = [{'match_id': 'm'}]
            worker._shadow = {('version', 'm'): {'match_id': 'm'}}
            with patch('service.model_service.write_inputs') as inputs, \
                    patch('service.model_service.write_predictions',
                          side_effect=[PostgresFailure('connection lost'), None]) as predictions:
                with self.assertRaises(PostgresFailure):
                    worker._flush_shadow()
                self.assertEqual(len(worker._inputs), 1)
                self.assertEqual(len(worker._shadow), 1)
                worker._flush_shadow()
            self.assertEqual(inputs.call_count, 1)
            self.assertEqual(predictions.call_count, 2)
            self.assertFalse(worker._inputs)
            self.assertFalse(worker._shadow)

    def test_postgres_input_failure_does_not_advance_replay_cursor(self):
        class PostgresFailure(Exception):
            pass
        with tempfile.TemporaryDirectory(dir=Path.cwd() / 'output', prefix='model-replay-') as root:
            ledger = Path(root)/'ledger'
            ledger.mkdir()
            path = ledger/'live-replay.jsonl.gz'
            HistoryJournal(path).append([decision()])
            path = next(ledger.glob('live-replay*.jsonl.gz'))
            worker = ModelService()
            worker.bind(str(ledger))
            with patch('service.model_service.write_inputs', side_effect=PostgresFailure('connection lost')):
                with self.assertRaises(PostgresFailure):
                    worker._consume_replay()
            self.assertNotIn(path.name, worker._cursor)
            self.assertTrue(worker._inputs)
            worker._consume_replay()
            self.assertEqual(worker._cursor[path.name], path.stat().st_size)
            self.assertFalse(worker._inputs)
            with sqlite3.connect(Path(root)/'models'/'inputs.sqlite3') as con:
                self.assertEqual(con.execute('SELECT count(*) FROM inputs').fetchone()[0], 20)

    def test_training_retains_all_qualification_counts_with_multi_decision_sample_cap(self):
        from service.model_training import TRAINING_MAX_ROWS_PER_MATCH
        self.assertEqual(TRAINING_MAX_ROWS_PER_MATCH, 40)

    def test_training_process_exit_status_is_actionable(self):
        self.assertEqual(ModelService._process_exit_error(1, False), '训练进程异常退出，退出码 1')
        self.assertEqual(ModelService._process_exit_error(-9, False), '训练进程被信号终止：SIGKILL (9)')
        from service.model_service import TRAINING_WALL_LIMIT_S
        self.assertEqual(ModelService._process_exit_error(-9, True),
                         '训练进程超过 %s 秒运行上限' % TRAINING_WALL_LIMIT_S)

    def test_incompatible_model_clears_previously_loaded_predictions(self):
        from service.model_training import atomic_json
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'models'/'active.json'
            model = approved_artifact('v', updated_at='2024-01-01T00:00:00+00:00')
            atomic_json(path,model)
            worker = ModelService()
            worker.bind(directory+'/ledger')
            self.assertTrue(worker.status()['model'])
            model.pop('data_version')
            atomic_json(path,model)
            worker._read()
            self.assertFalse(worker.status()['model'])
            worker.observe(decision())
            self.assertFalse(worker._shadow)

    def test_training_schedule_is_independent_of_execution_and_shadow_switches(self):
        for mode in ('algorithm', 'model', 'hybrid'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
                worker = ModelService()
                root = Path(directory) / 'ledger'
                cfg = replace(RuntimeConfig(), betting_enabled=False, algorithm_alert_enabled=False,
                              model_shadow_enabled=False, model_training_enabled=True, decision_mode=mode)
                process = MagicMock()
                with patch('service.model_service.subprocess.Popen', return_value=process) as spawn:
                    worker._start_training_check(root, cfg)
                self.assertIs(worker._process, process)
                spawn.assert_called_once()
                self.assertIn('service.model_training', spawn.call_args.args[0])

    def test_disabled_training_does_not_schedule_worker_even_when_execution_enabled(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            worker = ModelService()
            cfg = replace(RuntimeConfig(), betting_enabled=True, model_training_enabled=False)
            with patch('service.model_service.subprocess.Popen') as spawn:
                worker._start_training_check(Path(directory) / 'ledger', cfg)
            spawn.assert_not_called()

    def test_legacy_approval_cannot_be_loaded_as_active_model(self):
        from core.learned_model import DATA_VERSION, FEATURES
        from service.model_training import atomic_json
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / 'models' / 'active.json'
            model = {'features':list(FEATURES),'weights':[0]*len(FEATURES),'version':'legacy',
                     'data_version':DATA_VERSION,'feature_version':'market-offset-windows-v4',
                     'updated_at':'2024-01-01T00:00:00+00:00','promotion':{'approved':True}}
            atomic_json(path, model)
            worker = ModelService()
            worker.bind(str(Path(directory) / 'ledger'))
            status = worker.status()
            self.assertFalse(status['decision_model_available'])
            self.assertFalse(status['model'])
            self.assertIn('验证契约', status['training']['error'])
            self.assertEqual(json.loads(path.read_text()), model)

    def test_new_unapproved_candidate_is_available_only_for_shadow(self):
        from core.learned_model import DATA_VERSION, FEATURES
        from core.model_validation import VALIDATION_CONTRACT_VERSION
        from service.model_training import atomic_json
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            model = {'features':list(FEATURES),'weights':[0]*len(FEATURES),'version':'shadow-v1',
                     'data_version':DATA_VERSION,'feature_version':'market-offset-windows-v4',
                     'updated_at':'2024-01-01T00:00:00+00:00',
                     'validation_contract_version':VALIDATION_CONTRACT_VERSION,
                     'promotion':{'approved':False}}
            atomic_json(Path(directory) / 'models' / 'candidate-shadow.json', model)
            worker = ModelService()
            worker.bind(str(Path(directory) / 'ledger'))
            status = worker.status()
            self.assertTrue(status['shadow']['available'])
            self.assertEqual(status['shadow']['version'], 'shadow-v1')
            self.assertFalse(status['decision_model_available'])

    def test_shadow_evaluation_retains_separate_unit_return_summary_per_version(self):
        from service.model_service import hashlib_version
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            worker = ModelService()
            worker.bind(str(Path(directory) / 'ledger'))
            for version, net in (('v1', -1.0), ('v2', 0.9)):
                worker._shadow_model = {'version':version}
                worker._next_evaluation = 0.
                result = {'version':version,'unit_stake':{'stake':1.0,'net':net}}
                with patch('service.model_service.prospective_metrics', return_value=result):
                    worker._evaluate_shadow()
            for version, net in (('v1', -1.0), ('v2', 0.9)):
                path = Path(directory) / 'models' / ('shadow-metrics-' + hashlib_version(version) + '.json')
                self.assertEqual(json.loads(path.read_text())['unit_stake']['net'], net)

    def test_historical_shadow_keeps_grading_after_candidate_switch_without_replacing_live_summary(self):
        from service.model_service import hashlib_version
        from service.model_training import atomic_json
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            model_root = root / 'models'
            old = approved_artifact('v1', promotion={'approved': False})
            current = approved_artifact('v2', promotion={'approved': False},
                                        updated_at='2025-01-02T00:00:00+00:00')
            atomic_json(model_root / 'candidate-v1.json', old)
            atomic_json(model_root / 'candidate-v2.json', current)
            legacy = {**old, 'version':'legacy', 'validation_contract_version':'obsolete'}
            atomic_json(model_root / 'candidate-legacy.json', legacy)
            worker = ModelService()
            worker.bind(str(root / 'ledger'))
            worker._shadow_model = current
            worker._live_metrics = {'version':'v2', 'counts': {'pending':1}}
            before = dict(worker._live_metrics)
            first = {'version':'v1', 'status':'waiting', 'counts':{'pending':1},
                     'unit_stake':{'stake':0, 'net':0}}
            second = {'version':'v1', 'status':'ready', 'counts':{'settled':1},
                      'unit_stake':{'stake':1, 'net':.9}}
            with patch('service.model_service.prospective_metrics', side_effect=[first, second]) as evaluate:
                worker._evaluate_historical_shadow()
                worker._next_historical_evaluation = 0.
                worker._evaluate_historical_shadow()
            self.assertEqual(evaluate.call_count, 2)
            self.assertTrue(all(call.args[2]['version'] == 'v1' for call in evaluate.call_args_list))
            self.assertEqual(worker._live_metrics, before)
            summary = json.loads((model_root / ('shadow-metrics-' + hashlib_version('v1') + '.json')).read_text())
            self.assertEqual(summary['unit_stake']['net'], .9)
            self.assertEqual(summary['summary_schema_version'], 'versioned-shadow-metrics-v1')
            self.assertEqual(worker.status()['shadow']['historical_evaluation']['versions'], 1)
            self.assertEqual(json.loads((model_root / 'candidate-legacy.json').read_text()), legacy)

    def test_historical_shadow_refresh_is_bounded_to_one_version_per_cycle(self):
        from service.model_training import atomic_json
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            for version in ('v1', 'v2', 'v3'):
                atomic_json(root / 'models' / ('candidate-' + version + '.json'),
                            approved_artifact(version, promotion={'approved': False}))
            worker = ModelService()
            worker.bind(str(root / 'ledger'))
            worker._shadow_model = {'version':'v3'}
            with patch('service.model_service.prospective_metrics', return_value={'status':'waiting'}) as evaluate:
                worker._evaluate_historical_shadow()
                worker._evaluate_historical_shadow()
                self.assertEqual(evaluate.call_count, 1)
                worker._next_historical_evaluation = 0.
                worker._evaluate_historical_shadow()
            self.assertEqual([call.args[2]['version'] for call in evaluate.call_args_list], ['v1', 'v2'])

    def test_api_does_not_import_train_or_inference_or_launch_worker(self):
        result = subprocess.run([sys.executable, '-c',
            'import sys; from service.analysis import AnalysisService; '
            'assert "core.learned_model" not in sys.modules; '
            'assert "service.model_service" not in sys.modules; '
            'assert "store.training_data" not in sys.modules'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_incremental_journal_consumption_and_partial_member_retry(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = Path(root)/'ledger'
            ledger.mkdir()
            path = ledger/'live-replay.jsonl.gz'
            journal = HistoryJournal(path)
            journal.append([decision()])
            worker = ModelService()
            worker.bind(str(ledger))
            worker._consume_replay()
            worker._consume_replay()
            cursor = worker._cursor[path.name]
            self.assertEqual(cursor,path.stat().st_size)
            second = {**decision(),'computed_at':'2025-01-01T12:01:00+00:00'}
            member = gzip.compress((json.dumps(second)+'\n').encode())
            with path.open('ab') as f:
                f.write(member[:10])
            worker._consume_replay()
            self.assertEqual(worker._cursor[path.name],cursor)
            with path.open('ab') as f:
                f.write(member[10:])
            worker._consume_replay()
            self.assertEqual(worker._cursor[path.name],path.stat().st_size)
            other = ModelService()
            other.bind(str(ledger))
            other._consume_replay()
            self.assertEqual(other._cursor,worker._cursor)
            import sqlite3
            with sqlite3.connect(Path(root)/'models'/'inputs.sqlite3') as con:
                self.assertEqual(con.execute('SELECT count(*) FROM inputs').fetchone()[0],40)

    def test_recent_replay_is_consumed_before_large_historical_backlog(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = Path(root) / 'ledger'
            ledger.mkdir()
            old = ledger / 'live-replay.2026101000.jsonl.gz'
            recent = ledger / 'live-replay.2026101100.jsonl.gz'
            old_row = {**decision(), 'competition_type': 'virtual'}
            HistoryJournal(old).append([old_row])
            HistoryJournal(recent).append([decision()])
            import os
            os.utime(old, ns=(1_000_000_000, 1_000_000_000))
            worker = ModelService()
            worker.bind(str(ledger))
            worker._consume_replay()
            self.assertEqual(worker._cursor, {recent.name: recent.stat().st_size})
            self.assertEqual(worker._replay_stats['last_file'], recent.name)
            self.assertEqual(worker._replay_stats['last_batch_inputs'], 20)
            with sqlite3.connect(Path(root) / 'models' / 'inputs.sqlite3') as con:
                self.assertEqual(con.execute('SELECT count(*) FROM inputs').fetchone()[0], 20)

    def test_replay_reloads_cursor_after_repaired_gzip_replacement(self):
        from service.model_training import atomic_json
        with tempfile.TemporaryDirectory() as root:
            ledger = Path(root)/'ledger'
            ledger.mkdir()
            path = ledger/'live-replay.2025010100.jsonl.gz'
            path.write_bytes(gzip.compress((json.dumps(decision())+'\n').encode()))
            worker = ModelService()
            worker.bind(str(ledger))
            worker._consume_replay()
            cursor_path = Path(root)/'models'/'replay-cursor.json'
            revised = gzip.compress((json.dumps({**decision(),'computed_at':'2025-01-01T12:01:00+00:00'})+'\n').encode())
            path.write_bytes(revised)
            atomic_json(cursor_path, {path.name: len(revised)})
            worker._consume_replay()
            self.assertEqual(worker._cursor[path.name], len(revised))

    def test_replay_short_source_is_frozen_as_gap_without_resetting_cursor(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = Path(root) / 'ledger'
            ledger.mkdir()
            truncated = ledger / 'live-replay.2025010100.jsonl.gz'
            valid = ledger / 'live-replay.2025010101.jsonl.gz'
            HistoryJournal(truncated).append([decision()])
            HistoryJournal(valid).append([decision()])
            truncated.write_bytes(truncated.read_bytes()[:8])
            cursor_path = Path(root) / 'models' / 'replay-cursor.json'
            cursor_path.parent.mkdir()
            cursor_path.write_text(json.dumps({truncated.name: 1000}))
            worker = ModelService()
            worker.bind(str(ledger))
            worker._consume_replay()
            worker._consume_replay()
            self.assertEqual(worker._cursor[truncated.name], 1000)
            self.assertIn(truncated.name, worker._replay_gaps)
            self.assertEqual(worker._replay_gaps[truncated.name]['frozen'], True)
            self.assertTrue((Path(root) / 'models' / 'replay-gaps.json').exists())
            self.assertNotEqual(worker._replay_stats['gap_files'], 0)
