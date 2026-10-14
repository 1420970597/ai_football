from contextlib import closing
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from collector.leyu_realtime import TrendStore
from core.models import OddsSnapshot
from service.archive_service import backup_databases, cleanable, data_files, record_database_backups
from service import archive_service
from store.archive import S3Archive, UPLOAD_ATTEMPTS, journal_path, storage_mode
from store.snapshot_store import SnapshotStore


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.metadata = {}
        self.fail = False
        self.corrupt = False
        self.on_upload = None
        self.on_get = None
        self.upload_errors = []
        self.upload_payloads = []

    def upload_fileobj(self, stream, bucket, key, **kwargs):
        if self.upload_errors:
            self.upload_payloads.append(stream.read())
            raise self.upload_errors.pop(0)
        if self.fail:
            raise OSError("upload failed")
        self.objects[key] = stream.read()
        self.upload_payloads.append(self.objects[key])
        self.metadata[key] = kwargs["ExtraArgs"]["Metadata"]
        if self.on_upload:
            self.on_upload()

    def get_object(self, *, Bucket, Key, Range=None):
        if self.on_get:
            self.on_get()
        raw = self.objects[Key]
        if self.corrupt:
            raw = b"corrupt" + raw
        if Range:
            first, last = map(int, Range[6:].split("-"))
            raw = raw[first : last + 1]
        return {"Body": io.BytesIO(raw), "Metadata": self.metadata[Key]}

    def head_object(self, *, Bucket, Key):
        raw = self.objects[Key]
        if self.corrupt:
            raw = b'corrupt' + raw
        return {'ContentLength': len(raw), 'ETag': hashlib.md5(raw).hexdigest()}

    def get_paginator(self, operation):
        return self

    def paginate(self, **kwargs):
        return [{"Contents": [{"Key": key} for key in sorted(self.objects)]}]


class ArchiveTests(unittest.TestCase):
    def reader_state(self, path, **changes):
        models = self.root / 'models'
        models.mkdir(exist_ok=True)
        info = path.stat()
        live = {'schema_version': 2, 'cursor': {path.name: info.st_size},
                'inodes': {path.name: [info.st_dev, info.st_ino]}}
        dashboard = {'running': True, 'heartbeat_at': datetime.now(timezone.utc).isoformat(),
                     'replay': {'source_protocol': 1, 'source_backend': 's3_stream',
                                'remote_read_verified': True}}
        dashboard.update(changes)
        (models / 'live-shadow-cursor.json').write_text(json.dumps(live))
        (models / 'dashboard.json').write_text(json.dumps(dashboard))
        return live, dashboard

    def test_sealed_replay_early_cleanup_requires_fresh_verified_reader_and_live_eof(self):
        path = self.source('ledger/live-replay.2020010100.jsonl.gz')
        live, dashboard = self.reader_state(path)
        self.assertTrue(cleanable(self.root, path))
        model_root = self.root / 'models'
        for changes in ({'running': False},
                        {'heartbeat_at': (datetime.now(timezone.utc)-timedelta(seconds=91)).isoformat()},
                        {'heartbeat_at': (datetime.now(timezone.utc)+timedelta(seconds=5)).isoformat()},
                        {'heartbeat_at': 'invalid'},
                        {'replay': {'source_protocol': 1, 'source_backend': 'local', 'remote_read_verified': True}},
                        {'replay': {'source_protocol': 1, 'source_backend': 's3_stream', 'remote_read_verified': False}},
                        {'replay': {'source_protocol': True, 'source_backend': 's3_stream', 'remote_read_verified': True}}):
            with self.subTest(changes=changes):
                (model_root / 'dashboard.json').write_text(json.dumps({**dashboard, **changes}))
                self.assertFalse(cleanable(self.root, path))
        (model_root / 'dashboard.json').write_text(json.dumps(dashboard))
        for changes in ({'schema_version': 1}, {'cursor': {path.name: 0}},
                        {'inodes': {path.name: [0, 0]}}):
            with self.subTest(live=changes):
                (model_root / 'live-shadow-cursor.json').write_text(json.dumps({**live, **changes}))
                self.assertFalse(cleanable(self.root, path))

    def test_training_eof_fallback_and_active_hour_protection_remain(self):
        path = self.source('ledger/live-replay.2020010100.jsonl.gz')
        models = self.root / 'models'
        models.mkdir()
        (models / 'replay-cursor.json').write_text(json.dumps({path.name: path.stat().st_size}))
        (models / 'dashboard.json').write_text('{broken')
        self.assertTrue(cleanable(self.root, path))
        hour = datetime.now(timezone.utc).strftime('%Y%m%d%H')
        active = self.source('ledger/live-replay.'+hour+'.jsonl.gz')
        self.reader_state(active)
        (models / 'replay-cursor.json').write_text(json.dumps({active.name: active.stat().st_size}))
        self.assertFalse(cleanable(self.root, active))

    def test_malformed_reader_state_never_authorizes_early_cleanup(self):
        path = self.source('ledger/live-replay.2020010100.jsonl.gz')
        self.reader_state(path)
        for name in ('replay-cursor.json', 'live-shadow-cursor.json', 'dashboard.json'):
            state = self.root / 'models' / name
            before = state.read_text() if state.exists() else None
            state.write_text('{broken')
            if name != 'replay-cursor.json':
                self.assertFalse(cleanable(self.root, path))
            if before is None:
                state.unlink()
            else:
                state.write_text(before)

    def test_cleanup_network_and_source_hash_are_outside_short_replay_lock(self):
        path = self.source('ledger/live-replay.2020010100.jsonl.gz')
        self.archive.migrate([path])
        lock_path = self.root / 'models/replay.lock'
        lock_path.parent.mkdir()

        def free_lock():
            with lock_path.open('a') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
            return True

        network = []
        self.client.on_get = lambda: network.append(free_lock())
        guards = []

        def guard(_path):
            guards.append(free_lock())
            return True

        file_digest = hashlib.file_digest

        def local_hash(stream, name):
            self.assertTrue(free_lock())
            return file_digest(stream, name)

        with patch('store.archive.hashlib.file_digest', side_effect=local_hash):
            self.assertEqual(self.archive.cleanup([path], lock_path=lock_path, guard=guard)['files'], 1)
        self.assertTrue(network and all(network))
        self.assertEqual(guards, [True, False])
        self.assertFalse(path.exists())

    def test_cleanup_rechecks_guard_catalogue_and_inode_after_verification(self):
        for failure in ('guard', 'catalogue', 'inode'):
            with self.subTest(failure=failure):
                path = self.source('snapshots/'+failure+'.json', b'original')
                self.archive.migrate([path])
                original_stat = path.stat()
                state = {'touched': False}

                def change(failure=failure, path=path, original_stat=original_stat, state=state):
                    if state['touched']:
                        return
                    state['touched'] = True
                    if failure == 'catalogue':
                        with closing(self.archive._db()) as db, db:
                            db.execute('UPDATE files SET compressed=compressed+1 WHERE path=?',
                                       (path.relative_to(self.root).as_posix(),))
                    elif failure == 'inode':
                        replacement = path.with_suffix('.replacement')
                        replacement.write_bytes(b'original')
                        os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                        os.replace(replacement, path)

                self.client.on_get = change

                def guard(_path, failure=failure, state=state):
                    return not (failure == 'guard' and state['touched'])

                self.assertEqual(self.archive.cleanup([path], guard=guard)['files'], 0)
                self.assertEqual(path.read_bytes(), b'original')
                self.client.on_get = None

    def test_cleanup_revalidates_remote_on_later_calls_and_rejects_extra_bytes(self):
        path = self.source()
        self.archive.migrate([path])
        self.assertEqual(self.archive.cleanup([path], guard=lambda _path: False)['files'], 0)
        with closing(self.archive._db()) as db:
            key = db.execute('SELECT object_key FROM files').fetchone()[0]
        self.client.objects[key] += b'extra'
        with self.assertRaises(OSError):
            self.archive.cleanup([path])
        self.assertTrue(path.exists())

    def test_cleanup_reuses_pack_proof_only_inside_current_invocation(self):
        first = self.source('snapshots/first.json')
        second = self.source('snapshots/second.json')
        self.archive.migrate([first, second])
        with patch.object(self.archive, 'verify_pack', wraps=self.archive.verify_pack) as verify:
            self.archive.cleanup([first, second])
            self.assertEqual(verify.call_count, 1)
        self.archive.restore([first])
        with patch.object(self.archive, 'verify_pack', wraps=self.archive.verify_pack) as verify:
            self.archive.cleanup([first])
            self.assertEqual(verify.call_count, 1)

    def test_cleanup_without_etag_or_version_reverifies_each_member(self):
        first = self.source('snapshots/first.json')
        second = self.source('snapshots/second.json')
        self.archive.migrate([first, second])
        head = self.client.head_object

        def length_only(**kwargs):
            return {'ContentLength': head(**kwargs)['ContentLength']}

        with patch.object(self.client, 'head_object', side_effect=length_only), \
             patch.object(self.archive, 'verify_pack', wraps=self.archive.verify_pack) as verify:
            self.assertEqual(self.archive.cleanup([first, second])['files'], 2)
        self.assertEqual(verify.call_count, 2)

    def test_cleanup_reuses_one_connection_and_releases_transactions_before_io(self):
        paths = [self.source('snapshots/'+name+'.json') for name in ('first', 'second', 'missing')]
        self.archive.migrate(paths)
        paths[-1].unlink()
        connection = self.archive._db()

        class TransactionalConnection:
            """Make SELECT start a transaction, as a non-autocommit PG read does."""
            def execute(inner, sql, params=()):
                if not connection.in_transaction:
                    connection.execute('BEGIN')
                return connection.execute(sql, params)

            def commit(inner):
                connection.commit()

            def close(inner):
                connection.close()

            def __enter__(inner):
                return inner

            def __exit__(inner, *args):
                return connection.__exit__(*args)

        network = []

        def network_read():
            network.append(True)
            self.assertFalse(connection.in_transaction)

        self.client.on_get = network_read
        file_digest = hashlib.file_digest
        hashes = []

        def local_hash(stream, name):
            hashes.append(True)
            self.assertFalse(connection.in_transaction)
            return file_digest(stream, name)

        with patch.object(self.archive, '_db', return_value=TransactionalConnection()) as connect, \
             patch('store.archive.hashlib.file_digest', side_effect=local_hash):
            self.assertEqual(self.archive.cleanup(paths)['files'], 2)
        connect.assert_called_once_with()
        self.assertTrue(network)
        self.assertEqual(len(hashes), 2)
        with closing(self.archive._db()) as db:
            self.assertEqual(db.execute('SELECT SUM(cleaned) FROM files').fetchone()[0], 3)

    def test_cleanup_local_hash_mismatch_and_missing_remote_length_preserve_sources(self):
        path = self.source(raw=b'original')
        self.archive.migrate([path])
        before = path.stat()
        path.write_bytes(b'altered!')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.archive.cleanup([path])['files'], 0)
        self.assertTrue(path.exists())
        path.write_bytes(b'original')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with patch.object(self.client, 'head_object', return_value={'ETag': 'missing-length'}):
            with self.assertRaises(OSError):
                self.archive.cleanup([path])
        self.assertTrue(path.exists())

    def test_catalogued_sealed_replays_are_prioritized_before_snapshot_walk(self):
        snapshot = self.source('snapshots/a.json', b'snapshot')
        small = self.source('ledger/live-replay.2020010100.jsonl.gz', b'small')
        large = self.source('ledger/live-replay.2020010101.jsonl.gz', b'larger replay')
        self.archive.migrate([snapshot, small, large])
        ordered = []

        def capture(_root, _archive, paths):
            ordered.extend(paths)
            return {'files': 0, 'bytes': 0}

        with patch.object(archive_service, '_cleanup_paths', side_effect=capture):
            archive_service._cleanup_catalogued(self.root, self.archive)
        self.assertEqual(ordered, [large, small, snapshot])

    def test_cleanup_paths_processes_replay_group_before_ordinary_files(self):
        snapshot = self.source('snapshots/a.json', b'snapshot')
        replay = self.source('ledger/live-replay.2020010100.jsonl.gz', b'replay')
        self.reader_state(replay)
        calls = []

        def capture(paths, **kwargs):
            calls.append((paths, kwargs))
            return {'files': 0, 'bytes': 0}

        with patch.object(self.archive, 'cleanup', side_effect=capture):
            archive_service._cleanup_paths(self.root, self.archive, iter([snapshot, replay]))
        self.assertEqual([entry[0] for entry in calls], [[replay], [snapshot]])
        self.assertEqual(calls[0][1]['lock_path'], self.root / 'models/replay.lock')
        self.assertIsNone(calls[1][1]['lock_path'])
        self.assertTrue(calls[0][1]['guard'](replay))

    def test_upload_slowdown_retries_rewind_stream_and_are_bounded(self):
        class SlowDown(Exception):
            response = {'Error': {'Code': 'SlowDownWrite'}}

        raw = b'verified upload'
        digest = hashlib.sha256(raw).hexdigest()
        self.client.upload_errors = [SlowDown('private SDK response')] * 2
        with patch('store.archive.time.sleep') as sleep:
            self.archive._upload_verified('retry', io.BytesIO(raw), len(raw), digest)
        self.assertEqual(self.client.upload_payloads, [raw] * 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.client.upload_payloads.clear()
        self.client.upload_errors = [SlowDown()] * UPLOAD_ATTEMPTS
        with patch('store.archive.time.sleep') as sleep:
            with self.assertRaises(SlowDown):
                self.archive._upload_verified('exhausted', io.BytesIO(raw), len(raw), digest)
        self.assertEqual(self.client.upload_payloads, [raw] * UPLOAD_ATTEMPTS)
        self.assertEqual(sleep.call_count, UPLOAD_ATTEMPTS - 1)

    def test_upload_read_slowdown_rewinds_but_unrelated_failure_is_not_retried(self):
        class SlowDown(Exception):
            response = {'Error': {'Code': 'SlowDownRead'}}

        raw = b'upload'
        original = self.client.get_object
        failures = [SlowDown()]

        def get(**kwargs):
            if failures:
                raise failures.pop()
            return original(**kwargs)

        with patch.object(self.client, 'get_object', side_effect=get), patch('store.archive.time.sleep'):
            self.archive._upload_verified('read-retry', io.BytesIO(raw), len(raw), hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.client.upload_payloads, [raw, raw])
        self.client.upload_errors = [TypeError('programming defect')]
        with patch('store.archive.time.sleep') as sleep:
            with self.assertRaises(TypeError):
                self.archive._upload_verified('unrelated', io.BytesIO(raw), len(raw), hashlib.sha256(raw).hexdigest())
        sleep.assert_not_called()

    def test_archive_loop_reports_safe_retry_phase_and_recovers_without_deleting_input(self):
        path = self.source()
        captured = []
        handlers = {}
        failures = [True]

        class SlowDown(Exception):
            response = {'Error': {'Code': 'SlowDown'}}

        class Worker:
            def __init__(worker, root):
                worker.state = root / '_archive'

            def stats(worker):
                return {'backend': 's3', 'files': 0, 'bytes': 0}

            def migrate(worker, paths, cleanup=False):
                list(paths)
                if failures:
                    failures.pop()
                    raise SlowDown('secret response must never be exposed')
                handlers[signal.SIGTERM]()
                return {}

        writer = archive_service.atomic_json

        def capture(target, value):
            captured.append(json.loads(json.dumps(value)))
            writer(target, value)

        with patch.object(archive_service, 'S3Archive', Worker), \
             patch.object(archive_service, 'storage_mode', return_value='s3'), \
             patch.object(archive_service, 'database_backups_enabled', return_value=False), \
             patch.object(archive_service, 'data_files', side_effect=lambda _root: iter([path])), \
             patch.object(archive_service, 'atomic_json', side_effect=capture), \
             patch.object(archive_service.signal, 'signal', side_effect=lambda number, handler: handlers.update({number: handler})), \
             patch.object(archive_service, 'ARCHIVE_RETRY_BASE_S', .01):
            archive_service.run(self.root, cleanup=False, once=False)
        retry = next(item for item in captured if item.get('retrying'))
        self.assertEqual(retry['error_code'], 'SlowDown')
        self.assertEqual(retry['phase'], 'uploading')
        self.assertTrue(retry['running'])
        self.assertIn('heartbeat_at', retry)
        self.assertNotIn('secret response', json.dumps(captured))
        self.assertFalse(captured[-1]['running'])
        self.assertEqual(captured[-1]['error'], '')
        self.assertTrue(path.exists())

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
