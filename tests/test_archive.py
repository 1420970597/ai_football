from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from collector.leyu_realtime import TrendStore
from core.models import OddsSnapshot
from service.archive_service import backup_databases, cleanable, data_files, record_database_backups
from service import archive_service
from store.archive import S3Archive, journal_path, storage_mode
from store.snapshot_store import SnapshotStore


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.metadata = {}
        self.fail = False
        self.corrupt = False
        self.on_upload = None

    def upload_fileobj(self, stream, bucket, key, **kwargs):
        if self.fail:
            raise OSError("upload failed")
        self.objects[key] = stream.read()
        self.metadata[key] = kwargs["ExtraArgs"]["Metadata"]
        if self.on_upload:
            self.on_upload()

    def get_object(self, *, Bucket, Key, Range=None):
        raw = self.objects[Key]
        if self.corrupt:
            raw = b"corrupt" + raw
        if Range:
            first, last = map(int, Range[6:].split("-"))
            raw = raw[first : last + 1]
        return {"Body": io.BytesIO(raw), "Metadata": self.metadata[Key]}

    def get_paginator(self, operation):
        return self

    def paginate(self, **kwargs):
        return [{"Contents": [{"Key": key} for key in sorted(self.objects)]}]


class ArchiveTests(unittest.TestCase):
    def test_archive_worker_replaces_stale_failure_status_on_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "output"
            root.mkdir()
            stale = root / "_archive" / "status.json"
            stale.parent.mkdir()
            stale.write_text(json.dumps({"running": False, "error": "TypeError"}))

            class FakeArchive:
                def __init__(self, _root):
                    self.state = _root / "_archive"
                    self.state.mkdir(parents=True, exist_ok=True)

                def stats(self):
                    return {"backend": "s3", "files": 0, "bytes": 0}

                def migrate(self, _paths, cleanup=False):
                    return {"uploaded_files": 0, "uploaded_bytes": 0,
                            "cleaned_files": 0, "cleaned_bytes": 0}

            with patch.object(archive_service, "storage_mode", return_value="s3"), \
                 patch.object(archive_service, "S3Archive", FakeArchive), \
                 patch.object(archive_service, "data_files", return_value=iter(())):
                archive_service.run(root, cleanup=False, once=True)

            status = json.loads(stale.read_text())
            self.assertFalse(status["running"])
            self.assertEqual(status["error"], "")
    def test_verified_stream_and_revision_survive_catalog_rebuild(self):
        path = self.source('ledger/old.jsonl', b'old\n')
        self.archive.migrate([path], cleanup=True)
        with closing(self.archive._db()) as db:
            db.row_factory = sqlite3.Row
            original = dict(db.execute('SELECT * FROM files').fetchone())
        staged = self.root / 'stage'
        self.archive.copy_verified(original, staged)
        self.assertEqual(staged.read_bytes(), b'old\n')
        staged.write_bytes(b'corrected\n')
        self.archive.revise(path, staged)
        self.assertEqual(self.archive.read_many([path])[path], b'corrected\n')
        self.archive.rebuild_catalog()
        self.assertEqual(self.archive.read_many([path])[path], b'corrected\n')
        self.archive.copy_verified(original, staged)
        self.assertEqual(staged.read_bytes(), b'old\n')
        self.client.corrupt = True
        with self.assertRaises((OSError, __import__('zlib').error)):
            self.archive.copy_verified(original, staged)
        self.assertFalse(staged.exists())

    def test_parallel_ranges_preserve_compressed_member_order(self):
        raw = bytes(range(256))*100
        path = self.source('ledger/data.jsonl',raw)
        self.archive.migrate([path],cleanup=True)
        with closing(self.archive._db()) as db:
            db.row_factory = sqlite3.Row
            record = dict(db.execute('SELECT * FROM files').fetchone())
        target = self.root/'streamed'
        with patch('store.archive.READ_CHUNK_BYTES',13):
            self.archive.copy_verified(record,target)
            self.archive.verify_pack(record['object_key'])
        self.assertEqual(target.read_bytes(),raw)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "output"
        self.root.mkdir()
        self.client = FakeS3()
        self.archive = S3Archive(self.root, self.client)

    def source(self, name="snapshots/a.json", raw=b'{"value":1}'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def test_local_default_does_not_need_credentials_or_change_journals(self):
        self.assertEqual(storage_mode(self.root), "local")
        path = self.root / "ledger" / "live-replay.jsonl.gz"
        self.assertEqual(journal_path(path), path)
        (self.root / "storage-settings.json").write_text('{"backend":"invalid"}')
        with self.assertRaises(ValueError):
            storage_mode(self.root)

    def test_pack_verify_cleanup_range_read_restore_and_resume(self):
        a, b = self.source(), self.source("snapshots/b.json", b"other")
        self.archive.migrate([a, b], cleanup=True)
        self.assertFalse(a.exists())
        self.assertEqual(self.archive.read_many([a, b]), {a: b'{"value":1}', b: b"other"})
        self.assertEqual(self.archive.restore(), 2)
        self.assertTrue(a.exists())
        self.assertEqual(self.archive.migrate([a, b])["uploaded_files"], 0)
        # The permanent remote manifest recovers a lost local catalog.
        with closing(sqlite3.connect(self.archive.catalog)) as db, db:
            db.execute("DELETE FROM files")
            db.execute("DELETE FROM packs")
        self.assertEqual(self.archive.rebuild_catalog(), 2)
        self.assertEqual(self.archive.stats()["files"], 2)

    def test_upload_or_remote_checksum_failure_never_deletes_original(self):
        path = self.source()
        for flag in ("fail", "corrupt"):
            setattr(self.client, flag, True)
            with self.assertRaises(OSError):
                self.archive.migrate([path], cleanup=True)
            self.assertTrue(path.exists())
            self.assertEqual(self.archive.stats()["files"], 0)
            setattr(self.client, flag, False)

    def test_cleanup_recovers_catalog_after_unlink_before_commit(self):
        path = self.source()
        self.archive.migrate([path], cleanup=True)
        with closing(self.archive._db()) as db, db:
            db.execute("UPDATE files SET cleaned=0")
        self.assertFalse(path.exists())
        self.assertEqual(self.archive.cleanup([path]), {"files": 0, "bytes": 0})
        self.assertEqual(self.archive.stats()["cleaned_files"], 1)

    def test_stats_normalizes_postgres_decimal_totals(self):
        from decimal import Decimal
        from unittest.mock import MagicMock

        db = MagicMock()
        db.execute.side_effect = [
            MagicMock(fetchone=lambda: (Decimal("2"), Decimal("123"), Decimal("1"))),
            MagicMock(fetchone=lambda: (Decimal("1"), Decimal("456"))),
        ]
        self.archive.backend = "postgres"
        with patch.object(self.archive, "_db", return_value=db):
            stats = self.archive.stats()

        self.assertEqual(stats["bytes"], 123)
        self.assertEqual(stats["object_bytes"], 456)

    def test_changed_original_is_not_removed_and_remote_failure_blocks_cleanup(self):
        path = self.source()
        self.client.on_upload = lambda: path.write_bytes(b"changed original")
        self.archive.migrate([path], cleanup=True)
        self.assertEqual(path.read_bytes(), b"changed original")
        self.client.on_upload = None
        self.archive.migrate([path])
        other = S3Archive(self.root, self.client)
        self.client.corrupt = True
        with self.assertRaises(OSError):
            other.cleanup([path])
        self.assertTrue(path.exists())

    def test_snapshot_reads_after_local_cleanup(self):
        store = SnapshotStore(self.root / "snapshots")
        snapshot = OddsSnapshot(
            match_id="m",
            league="l",
            home="h",
            away="a",
            market="HAD",
            outcomes=("home", "draw", "away"),
            odds=(2.0, 3.0, 4.0),
            source="s",
        )
        path = store.append(snapshot)
        self.archive.migrate([path], cleanup=True)
        with patch("store.archive.archive_for", return_value=self.archive):
            rows = store.load_match("s", "l", "m")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].odds, (2.0, 3.0, 4.0))

    def test_s3_trend_segments_retain_legacy_history_after_cleanup(self):
        (self.root / "storage-settings.json").write_text('{"backend":"s3"}')
        legacy = self.source("_trends/m.jsonl", b'{"n":1}\n')
        recent = self.source("_trends/m/2026101009.jsonl", b'{"n":2}\n')
        self.archive.migrate([legacy, recent], cleanup=True)
        trends = TrendStore(str(self.root / "_trends"))
        with patch("store.archive.archive_for", return_value=self.archive):
            self.assertEqual(trends.load("m", limit=2), [{"n": 1}, {"n": 2}])
        trends._buf = {"m": ['{"n":3}']}
        trends.flush()
        self.assertFalse(legacy.exists())
        self.assertEqual(len(list((self.root / "_trends" / "m").glob("*.jsonl"))), 1)

    def test_cleanup_excludes_operational_state_and_unconsumed_replay(self):
        db = self.source("betting-orders.sqlite3")
        self.assertFalse(cleanable(self.root, db))
        index = self.source("snapshots/a/_index.json")
        self.assertFalse(cleanable(self.root, index))
        replay = self.source("ledger/live-replay.2020010100.jsonl.gz")
        self.assertFalse(cleanable(self.root, replay))
        cursor = self.root / "models" / "replay-cursor.json"
        cursor.parent.mkdir()
        cursor.write_text(json.dumps({replay.name: replay.stat().st_size}))
        self.assertTrue(cleanable(self.root, replay))

    def test_database_backups_are_daily_and_repair_files_are_migrated(self):
        source = self.root / "ledger" / "ledger.sqlite3"
        source.parent.mkdir(parents=True)
        with sqlite3.connect(source) as db:
            db.execute("create table decisions (id integer)")
            db.execute("insert into decisions values (1)")
        backups = backup_databases(self.root)
        self.assertEqual(len(backups), 1)
        record_database_backups(self.root, backups)
        backups[0].unlink()
        self.assertEqual(backup_databases(self.root), [])

        repair = self.root / "_archive" / "repairs" / "task42" / "catalog.sqlite3"
        repair.parent.mkdir(parents=True)
        repair.write_bytes(b"repair")
        paths = list(data_files(self.root))
        self.assertIn(repair, paths)
        self.assertTrue(cleanable(self.root, repair))
