"""Offline replay transport regressions; no PostgreSQL/S3 credentials or calls."""
from dataclasses import replace
import gzip
import hashlib
import io
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

from store.replay_source import (DECODE_CHUNK_BYTES, REPLAY_SOURCE_PROTOCOL,
                                 REWIND_BYTES, ReplaySource, ReplaySources)


class FakeCatalog:
    def __init__(self, archive):
        self.archive = archive
        self.connection = sqlite3.connect(':memory:')

        def ignore_sentinel(left, right):
            left, right = left.replace('\uffff', ''), right.replace('\uffff', '')
            return (left > right) - (left < right)

        self.connection.create_collation('path_locale', ignore_sentinel)
        self.connection.execute(
            'CREATE TABLE files(path TEXT PRIMARY KEY COLLATE path_locale, object_key TEXT, '
            '"offset" INTEGER, compressed INTEGER, size INTEGER, sha256 TEXT, mtime_ns INTEGER)')
        columns = ('path', 'object_key', 'offset', 'compressed', 'size', 'sha256', 'mtime_ns')
        self.connection.executemany('INSERT INTO files VALUES(?,?,?,?,?,?,?)', [
            tuple(record[key] for key in columns) for record in archive.records.values()])

    def execute(self, sql, params):
        self.archive.catalog_reads += 1
        if self.archive.catalog_error:
            raise OSError('private database endpoint')
        return self.connection.execute(sql, params)

    def close(self):
        self.connection.close()


class FakeArchive:
    def __init__(self, root):
        self.root = root
        self.records = {}
        self.packs = {}
        self.requests = []
        self.closed_streams = 0
        self.catalog_reads = 0
        self.catalog_error = False
        self.range_error = False
        self.range_transform = lambda raw: raw
        self.chunk_bytes = 8192

    def add(self, name, raw, *, mtime=100, local=False, directory='ledger'):
        wire = gzip.compress(raw, mtime=0)
        key = f'pack-{len(self.packs)}'
        prefix = b'other-pack-member'
        self.packs[key] = prefix + wire + b'next-pack-member'
        record = {'path': (Path(directory) / name).as_posix(), 'object_key': key, 'offset': len(prefix),
                  'compressed': len(wire), 'size': len(raw),
                  'sha256': hashlib.sha256(raw).hexdigest(), 'mtime_ns': mtime}
        self.records[record['path']] = record
        path = self.root / directory / name
        if local:
            path.write_bytes(raw)
            import os
            os.utime(path, ns=(mtime, mtime))
        return ReplaySource(path, len(raw), mtime, True, dict(record))

    def _db(self):
        return FakeCatalog(self)

    def _range_blocks(self, key, start, length):
        self.requests.append((key, start, length))
        try:
            if self.range_error:
                raise OSError('private s3 endpoint')
            raw = self.range_transform(self.packs[key][start:start + length])
            for offset in range(0, len(raw), self.chunk_bytes):
                yield raw[offset:offset + self.chunk_bytes]
        finally:
            self.closed_streams += 1


def read_all(stream, size=64 * 1024):
    blocks = []
    while block := stream.read(size):
        blocks.append(block)
    return b''.join(blocks)


class ReplaySourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'output'
        self.ledger = self.root / 'ledger'
        self.ledger.mkdir(parents=True)
        self.archive = FakeArchive(self.root)
        self.sources = ReplaySources(self.ledger, self.archive)
        self.addCleanup(self.sources.close)
        self.raw = random.Random(52).randbytes(512 * 1024)

    def add(self, hour='2020010100', raw=None, **kwargs):
        return self.archive.add('live-replay.' + hour + '.jsonl.gz',
                                self.raw if raw is None else raw, **kwargs)

    def test_local_mode_is_lazy_and_requires_no_archive_initialization(self):
        path = self.ledger / 'live-replay.jsonl.gz'
        path.write_bytes(b'local source')
        with patch('store.replay_source.archive_for', return_value=None) as factory:
            sources = ReplaySources(self.ledger)
            self.addCleanup(sources.close)
            factory.assert_not_called()
            listed = sources.list_sources()
            self.assertEqual(len(listed), 1)
            self.assertFalse(listed[0].remote)
            with sources.open(listed[0], 6) as stream:
                self.assertEqual(stream.read(), b'source')
            self.assertIsNone(sources.probe_source())
            factory.assert_called_once_with(self.ledger)
        self.assertEqual(REPLAY_SOURCE_PROTOCOL, 1)

    def test_union_prefers_local_but_probe_forces_remote_even_when_local_exists(self):
        self.add('2020010100', local=True)
        remote = self.add('2020010101', mtime=200)
        local = self.ledger / 'live-replay.2020010102.jsonl.gz'
        local.write_bytes(b'current source')
        (self.ledger / 'unrelated.jsonl.gz').write_bytes(b'ignore')
        listed = self.sources.list_sources()
        self.assertEqual(len(listed), 3)
        self.assertFalse(next(row for row in listed if row.name.endswith('0100.jsonl.gz')).remote)
        self.assertTrue(next(row for row in listed if row.name == remote.name).remote)
        self.assertTrue(self.sources.probe_source().remote)
        self.assertEqual(self.archive.requests, [])

    def test_real_sql_catalog_ignoring_unicode_sentinel_finds_remote_journals(self):
        source = self.add()
        database = self.archive._db()
        try:
            old = database.execute('SELECT path FROM files WHERE path>=? AND path<?',
                                   ('ledger/', 'ledger/\uffff')).fetchall()
        finally:
            database.close()
        self.assertEqual(old, [])
        self.assertEqual(self.sources.list_sources(), [source])
        self.assertEqual(self.sources.probe_source(), source)

    def test_remote_directory_like_wildcards_backslash_and_case_are_literal(self):
        directory = r'l_ed%g\er'
        ledger = self.root / directory
        ledger.mkdir()
        name = 'live-replay.2020010100.jsonl.gz'
        expected = self.archive.add(name, self.raw, directory=directory)
        for other in (r'lXedYYg\er', r'l_ed%ger', r'L_ed%g\er', directory + 'suffix'):
            self.archive.add(name, self.raw, directory=other)
        sources = ReplaySources(ledger, self.archive)
        self.addCleanup(sources.close)
        self.assertEqual(sources.list_sources(), [expected])
        self.assertEqual(sources.probe_source(), expected)
        self.assertEqual(self.archive.requests, [])

    def test_remote_original_gzip_bytes_and_resume_do_not_redownload_prefix(self):
        source = self.add()
        with self.sources.open(source, 180000) as stream:
            self.assertEqual(stream.read(20000), self.raw[180000:200000])
            self.assertEqual(stream.tell(), 200000)
            self.assertFalse(stream.verified)
        with self.sources.open(source, 200000) as stream:
            self.assertEqual(stream.read(10000), self.raw[200000:210000])
        self.assertEqual(len(self.archive.requests), 1)

    def test_small_backward_seek_uses_rewind_and_big_backward_seek_reopens_once(self):
        source = self.add()
        with self.sources.open(source, 200000) as stream:
            stream.seek(-REWIND_BYTES, io.SEEK_CUR)
            self.assertEqual(stream.read(100), self.raw[200000 - REWIND_BYTES:200100 - REWIND_BYTES])
            self.assertEqual(len(self.archive.requests), 1)
            stream.seek(0)
            self.assertEqual(stream.read(100), self.raw[:100])
            self.assertEqual(len(self.archive.requests), 2)

    def test_remote_eof_checks_source_sha_and_outer_gzip_crc(self):
        source = self.add()
        with self.sources.open(source) as stream:
            self.assertEqual(hashlib.sha256(read_all(stream)).hexdigest(), hashlib.sha256(self.raw).hexdigest())
            self.assertTrue(stream.verified)
            self.assertEqual(stream.tell(), len(self.raw))
            self.assertEqual(stream.read(1), b'')
        self.assertEqual(self.archive.closed_streams, 1)

    def test_empty_remote_source_has_verified_zero_length_eof(self):
        source = self.add(raw=b'')
        with self.sources.open(source) as stream:
            self.assertEqual(stream.read(1), b'')
            self.assertEqual(stream.tell(), 0)
            self.assertTrue(stream.verified)

    def test_inner_concatenated_gzip_members_parse_after_local_source_is_absent(self):
        row1, row2 = {'event': 1}, {'event': 2}
        first = gzip.compress((json.dumps(row1) + '\n').encode(), mtime=0)
        second = gzip.compress((json.dumps(row2) + '\n').encode(), mtime=0)
        source = self.add(raw=first + second)
        self.assertFalse(source.path.exists())
        with self.sources.open(source, len(first)) as stream:
            decoder = zlib.decompressobj(31)
            parsed = decoder.decompress(stream.read(64 * 1024))
            self.assertTrue(decoder.eof)
            stream.seek(stream.tell() - len(decoder.unused_data))
            self.assertEqual(json.loads(parsed), row2)
            self.assertEqual(stream.read(1), b'')

    def test_decoded_output_and_rewind_memory_are_bounded_for_high_expansion(self):
        raw = b'x' * (DECODE_CHUNK_BYTES * 9 + 3)
        source = self.add(raw=raw)
        total = 0
        with self.sources.open(source) as stream:
            while block := stream.read():
                self.assertLessEqual(len(block), DECODE_CHUNK_BYTES)
                self.assertLessEqual(len(stream._buffer), DECODE_CHUNK_BYTES + REWIND_BYTES)
                total += len(block)
            self.assertEqual(total, len(raw))
            self.assertTrue(stream.verified)

    def test_seek_end_checks_eof_and_invalid_seeks_do_not_escape_source(self):
        source = self.add()
        with self.sources.open(source) as stream:
            self.assertEqual(stream.seek(-7, io.SEEK_END), len(self.raw) - 7)
            self.assertEqual(stream.read(7), self.raw[-7:])
            self.assertTrue(stream.verified)
            for offset, whence in ((-1, io.SEEK_SET), (len(self.raw) + 1, io.SEEK_SET), (0, 99)):
                with self.subTest(offset=offset, whence=whence), self.assertRaises(ValueError):
                    stream.seek(offset, whence)

    def test_checksum_mismatch_is_rejected_and_failed_reader_is_not_cached(self):
        source = self.add()
        bad = replace(source, record={**source.record, 'sha256': '0' * 64})
        with self.assertRaisesRegex(OSError, 'SHA256'), self.sources.open(bad) as stream:
            read_all(stream)
        self.assertEqual(len(self.sources._readers), 0)
        with self.sources.open(source) as stream:
            self.assertEqual(len(read_all(stream)), len(self.raw))
        self.assertEqual(len(self.archive.requests), 2)

    def test_truncated_wrong_and_extra_ranges_never_report_verified_eof(self):
        source = self.add()
        variants = {
            'truncated': lambda raw: raw[:-3],
            'wrong-range': lambda raw: b'not-gzip' + raw[8:],
            'extra-range': lambda raw: raw + b'x',
            'bad-crc': lambda raw: raw[:-8] + bytes([raw[-8] ^ 1]) + raw[-7:],
        }
        for name, transform in variants.items():
            with self.subTest(name=name):
                self.archive.range_transform = transform
                with self.assertRaises(OSError), self.sources.open(source) as stream:
                    read_all(stream)
                self.assertFalse(self.sources._readers)

    def test_declared_source_size_mismatch_is_rejected(self):
        source = self.add()
        for size in (source.size - 1, source.size + 1):
            bad = replace(source, size=size, record={**source.record, 'size': size})
            with self.subTest(size=size), self.assertRaises(OSError), self.sources.open(bad) as stream:
                read_all(stream)

    def test_range_errors_are_safe_and_retry_opens_a_new_forward_stream(self):
        source = self.add()
        self.archive.range_error = True
        with self.assertRaisesRegex(OSError, 'S3 回放归档范围读取失败') as captured:
            with self.sources.open(source) as stream:
                stream.read(1)
        self.assertNotIn('private', str(captured.exception))
        self.archive.range_error = False
        with self.sources.open(source) as stream:
            self.assertEqual(stream.read(10), self.raw[:10])
        self.assertEqual(len(self.archive.requests), 2)

    def test_catalog_or_explicit_s3_initialization_failure_is_not_silent_local_fallback(self):
        with patch('store.replay_source.archive_for', side_effect=OSError('private endpoint')):
            sources = ReplaySources(self.ledger)
            with self.assertRaisesRegex(OSError, 'S3 回放源初始化失败'):
                sources.list_sources()
        self.archive.catalog_error = True
        with self.assertRaisesRegex(OSError, 'S3 回放源目录读取失败'):
            self.sources.list_sources()

    def test_two_forward_readers_are_reused_and_lru_eviction_closes_the_oldest(self):
        first, second, third = self.add('2020010100'), self.add('2020010101'), self.add('2020010102')
        for source in (first, second, first, third):
            with self.sources.open(source, 100) as stream:
                self.assertEqual(stream.read(10), self.raw[100:110])
        self.assertEqual(len(self.sources._readers), 2)
        self.assertEqual(len(self.archive.requests), 3)
        self.assertEqual(self.archive.closed_streams, 1)
        self.sources.close()
        self.assertEqual(self.archive.closed_streams, 3)

    def test_changed_fingerprint_reopens_the_same_logical_source(self):
        first = self.add()
        with self.sources.open(first) as stream:
            stream.read(10)
        second = self.add(raw=b'y' * len(self.raw), mtime=200)
        with self.sources.open(second) as stream:
            self.assertEqual(stream.read(10), b'y' * 10)
        self.assertEqual(len(self.archive.requests), 2)
        self.assertEqual(len(self.sources._readers), 1)
        self.assertEqual(self.archive.closed_streams, 1)

    def test_each_remote_revision_component_invalidates_a_cached_forward_reader(self):
        source = self.add()
        alternate_key = 'alternate-pack'
        self.archive.packs[alternate_key] = self.archive.packs[source.record['object_key']]
        for field, value in (('object_key', alternate_key), ('sha256', '0' * 64),
                             ('size', source.size + 1), ('mtime_ns', source.mtime_ns + 1)):
            with self.subTest(field=field):
                sources = ReplaySources(self.ledger, self.archive)
                try:
                    with sources.open(source, 100) as stream:
                        stream.read(1)
                    requests = len(self.archive.requests)
                    record = {**source.record, field: value}
                    revised = replace(source, size=record['size'], mtime_ns=record['mtime_ns'], record=record)
                    with sources.open(revised, 100) as stream:
                        self.assertEqual(stream.read(1), self.raw[100:101])
                    self.assertEqual(len(self.archive.requests), requests + 1)
                finally:
                    sources.close()

    def test_deleted_local_source_resumes_matching_remote_revision(self):
        remote = self.add(local=True)
        local = self.sources.list_sources()[0]
        self.assertFalse(local.remote)
        local.path.unlink()
        with self.sources.open(local, 180000) as stream:
            self.assertEqual(stream.read(10), self.raw[180000:180010])
        self.assertEqual(self.sources.probe_source().name, remote.name)

    def test_deleted_local_source_rejects_unmatched_revision(self):
        self.add(local=True)
        local = self.sources.list_sources()[0]
        local.path.unlink()
        self.archive.records['ledger/' + local.name]['mtime_ns'] += 1
        with self.assertRaisesRegex(OSError, '无匹配的远端版本'), self.sources.open(local):
            pass

    def test_close_disallows_future_reads_and_new_contexts(self):
        source = self.add()
        with self.sources.open(source) as stream:
            stream.read(10)
        self.sources.close()
        with self.assertRaises(ValueError):
            stream.read(1)
        with self.assertRaises(ValueError), self.sources.open(source):
            pass
        with self.assertRaises(ValueError):
            self.sources.list_sources()

    def test_close_during_lazy_initialization_does_not_create_a_late_reader(self):
        source = self.add()
        sources = ReplaySources(self.ledger)
        started, release = threading.Event(), threading.Event()
        failures = []

        def initialize(_ledger):
            started.set()
            release.wait(2)
            return self.archive

        def consume():
            try:
                with sources.open(source) as stream:
                    stream.read(1)
            except Exception as exc:
                failures.append(exc)

        with patch('store.replay_source.archive_for', side_effect=initialize):
            thread = threading.Thread(target=consume)
            thread.start()
            try:
                self.assertTrue(started.wait(1))
                sources.close()
            finally:
                release.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ValueError)
        self.assertEqual(self.archive.requests, [])
        self.assertFalse(sources._readers)

    def test_invalid_record_and_offset_are_rejected_before_download(self):
        source = self.add()
        for field, value in (('offset', -1), ('compressed', 0), ('sha256', 'invalid')):
            bad = replace(source, record={**source.record, field: value})
            with self.subTest(field=field), self.assertRaises(OSError), self.sources.open(bad):
                pass
        for offset in (-1, source.size + 1, True, .5):
            with self.subTest(offset=offset), self.assertRaises(ValueError), self.sources.open(source, offset):
                pass
        self.assertEqual(self.archive.requests, [])
