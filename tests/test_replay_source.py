import gzip
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from store.replay_source import ReplaySource, ReplaySources


class _RangeArchive:
    def __init__(self, payload: bytes):
        self.root = Path('/tmp/replay-source-test-output')
        self.payload = gzip.compress(payload, mtime=0)

    def _range_blocks(self, _key: str, start: int, length: int):
        yield self.payload[start:start + length]


class ReplaySourceTests(unittest.TestCase):
    def test_remote_reader_streams_and_rewinds_with_integrity_checks(self):
        payload = b'{"match_id":"m"}\n' * 200
        archive = _RangeArchive(payload)
        source = ReplaySource(
            archive.root / 'ledger' / 'live-replay.2026101500.jsonl.gz',
            len(payload), 7, True,
            {'object_key': 'pack', 'offset': 0, 'compressed': len(archive.payload),
             'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(), 'mtime_ns': 7},
        )
        sources = ReplaySources(archive.root / 'ledger', archive)
        with sources.open(source) as stream:
            self.assertEqual(stream.read(31), payload[:31])
            stream.seek(0)
            self.assertEqual(stream.read(), payload)
            with self.assertRaises(ValueError):
                stream.seek(len(payload) + 1)
        sources.close()

    def test_catalog_presence_does_not_establish_remote_readiness(self):
        archive = _RangeArchive(b'probe')
        source = ReplaySource(archive.root/'ledger'/'live-replay.jsonl.gz', 5, 7, True,
            {'object_key':'pack','offset':0,'compressed':len(archive.payload),'size':5,
             'sha256':hashlib.sha256(b'probe').hexdigest(),'mtime_ns':7})
        sources = ReplaySources(archive.root/'ledger',archive)
        self.assertFalse(sources.remote_read_verified)
        with patch.object(sources,'_remote_sources',return_value=[source]):
            self.assertTrue(sources.verify_remote_read())
        sources._last_remote_probe = 0
        with patch.object(sources,'_remote_sources',side_effect=OSError('offline')):
            self.assertFalse(sources.verify_remote_read())
        sources.close()

    def test_local_source_is_used_when_remote_sdk_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory)
            path = ledger / 'live-replay.jsonl.gz'
            path.write_bytes(gzip.compress(b'row\n'))
            sources = ReplaySources(ledger)
            listed = sources.list_sources()
            self.assertEqual([item.name for item in listed], [path.name])
            with sources.open(listed[0]) as stream:
                self.assertEqual(stream.read(), path.read_bytes())
            sources.close()

    def test_identity_remote_reader_preserves_original_gzip_bytes(self):
        original = gzip.compress(b'row\n' * 100, mtime=0)
        archive = _RangeArchive(b'ignored')
        archive.payload = original
        source = ReplaySource(
            archive.root / 'ledger' / 'live-replay.2026101501.jsonl.gz',
            len(original), 8, True,
            {'object_key': 'pack', 'offset': 0, 'compressed': len(original),
             'size': len(original), 'sha256': hashlib.sha256(original).hexdigest(),
             'mtime_ns': 8, 'encoding': 'identity'},
        )
        with ReplaySources(archive.root / 'ledger', archive).open(source) as stream:
            self.assertEqual(stream.read(), original)


if __name__ == '__main__':
    unittest.main()
