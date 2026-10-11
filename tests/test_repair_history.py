import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from store.archive import S3Archive
from store.ledger_db import LedgerDB
from tests.test_archive import FakeS3
from tests.test_training_data import decision
from tools.repair_history import install_local_revision, run


class RepairHistoryToolTests(unittest.TestCase):
    def test_s3_only_journal_is_repaired_consumed_and_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'output'
            path = root/'ledger'/'live-replay.2025010100.jsonl.gz'
            path.parent.mkdir(parents=True)
            ledger = LedgerDB(path.parent/'ledger.sqlite3')
            path.write_bytes(gzip.compress((json.dumps(decision())+'\n').encode()))
            archive = S3Archive(root, FakeS3())
            archive.migrate([path],cleanup=True)
            state = root/'_archive'/'repairs'/'task42'
            with patch('tools.repair_history.S3Archive',return_value=archive):
                run(root,state,Path(directory)/'backup')
                revised = json.loads(gzip.decompress(archive.read_many([path])[path]))
                self.assertEqual(revised['repair_status'],'already_current')
                self.assertIn('sha256',revised['repair_source'])
                with sqlite3.connect(root/'models'/'inputs.sqlite3') as con:
                    self.assertEqual(con.execute('SELECT count(*) FROM inputs').fetchone()[0],8)
                count = archive.stats()['packs']
                run(root,state,Path(directory)/'backup')
                # Source revisions are not republished on resume.
                with sqlite3.connect(state/'progress.sqlite3') as con:
                    self.assertEqual(con.execute("SELECT count(*) FROM sources WHERE status='repaired'").fetchone()[0],1)
                status = json.loads((state/'status.json').read_text())
                self.assertEqual(status['phase'], 'completed')
                self.assertEqual(status['remaining_files'], 0)
                completion = json.loads((state/'completion.json').read_text())
                self.assertEqual(completion['counts']['repaired'], 1)
                self.assertLessEqual(archive.stats()['packs'],count+1)
            ledger.connection.close()

    def test_installing_repaired_replay_advances_cursor_under_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'output'
            local = root/'ledger'/'live-replay.2025010100.jsonl.gz'
            local.parent.mkdir(parents=True)
            local.write_bytes(gzip.compress(b'{"original":true}\n'))
            stat = local.stat()
            staged = root/'_archive'/'repairs'/'task42'/'revision.jsonl.gz'
            staged.parent.mkdir(parents=True)
            staged.write_bytes(gzip.compress(b'{"repaired":true}\n'))
            status = install_local_revision(
                root, staged.parent, local, staged,
                {'size':stat.st_size, 'mtime_ns':stat.st_mtime_ns}, replay=True)
            self.assertEqual(status, 'installed')
            self.assertEqual(gzip.decompress(local.read_bytes()), b'{"repaired":true}\n')
            cursor = json.loads((root/'models'/'replay-cursor.json').read_text())
            self.assertEqual(cursor[local.name], local.stat().st_size)
