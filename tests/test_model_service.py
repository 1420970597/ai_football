from pathlib import Path
import gzip
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from service.model_service import ModelService
from store.history import HistoryJournal
from tests.test_training_data import decision


class ModelServiceTests(unittest.TestCase):
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
                self.assertEqual(con.execute('SELECT count(*) FROM inputs').fetchone()[0], 8)

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
        from core.learned_model import DATA_VERSION, FEATURES
        from service.model_training import atomic_json
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'models'/'active.json'
            model = {'features':list(FEATURES),'weights':[0]*len(FEATURES),'version':'v',
                     'data_version':DATA_VERSION,'feature_version':'market-offset-windows-v4','updated_at':'2024-01-01'}
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
                self.assertEqual(con.execute('SELECT count(*) FROM inputs').fetchone()[0],16)

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
            self.assertEqual(worker._replay_stats['last_batch_inputs'], 8)
            with sqlite3.connect(Path(root) / 'models' / 'inputs.sqlite3') as con:
                self.assertEqual(con.execute('SELECT count(*) FROM inputs').fetchone()[0], 8)

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
