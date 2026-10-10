from pathlib import Path
import gzip
import json
import subprocess
import sys
import tempfile
import unittest

from service.model_service import ModelService
from store.history import HistoryJournal
from tests.test_training_data import decision


class ModelServiceTests(unittest.TestCase):
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
