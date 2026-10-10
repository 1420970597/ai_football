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
from service.archive_service import cleanable
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
