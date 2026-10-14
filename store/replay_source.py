"""Bounded replay readers over local journals and immutable S3 pack members.

Offsets always address the original journal's gzip bytes. The archive's outer
gzip is decoded incrementally; reading a batch does not restore a journal or
repeat its prefix. Full source integrity is established only at remote EOF.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
import re
import threading
from typing import Any, Iterator, Mapping
import zlib

from store.archive import archive_for

REPLAY_SOURCE_PROTOCOL = 1
DECODE_CHUNK_BYTES = 128 * 1024
REWIND_BYTES = 64 * 1024
MAX_FORWARD_READERS = 2
_RECORD_COLUMNS = ('path', 'object_key', 'offset', 'compressed', 'size', 'sha256', 'mtime_ns')


@dataclass(frozen=True)
class ReplaySource:
    path: Path
    size: int
    mtime_ns: int
    remote: bool = False
    record: Mapping[str, Any] | None = None

    @property
    def name(self) -> str:
        return self.path.name


class _RemoteReplayReader:
    """Forward decoder with a small rewind window for inner-gzip read-ahead.

    ``read`` may return a short chunk, including for an unbounded request. This
    deliberately bounds each decoded output to 128 KiB. Callers stream until
    EOF. The existing journal parser requests at most 64 KiB per read.
    """

    def __init__(self, archive: Any, source: ReplaySource) -> None:
        self.archive = archive
        self.source = source
        self.record = dict(source.record or {})
        if (isinstance(source.size, bool) or not isinstance(source.size, int) or source.size < 0
                or self.record.get('size') != source.size
                or self.record.get('mtime_ns') != source.mtime_ns):
            raise OSError('回放归档版本元数据无效')
        for field in ('offset', 'compressed'):
            value = self.record.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise OSError('回放归档范围元数据无效')
        if (not self.record['compressed'] or not self.record.get('object_key')
                or not isinstance(self.record['object_key'], str)):
            raise OSError('回放归档范围元数据无效')
        digest = self.record.get('sha256')
        if not isinstance(digest, str) or re.fullmatch(r'[0-9a-fA-F]{64}', digest) is None:
            raise OSError('回放归档校验元数据无效')
        self.closed = False
        self._blocks: Iterator[bytes] | None = None
        self._restart()

    def _check_open(self) -> None:
        if self.closed:
            raise ValueError('回放读取流已关闭')

    def _close_blocks(self) -> None:
        blocks, self._blocks = self._blocks, None
        close = getattr(blocks, 'close', None)
        if close is not None:
            try:
                close()
            except (ValueError, RuntimeError):
                # Another thread can be inside the SDK future. Marking this
                # reader closed still prevents consumption when it returns;
                # the existing transport timeout bounds that in-flight call.
                pass

    def _restart(self) -> None:
        self._close_blocks()
        try:
            self._blocks = iter(self.archive._range_blocks(
                self.record['object_key'], self.record['offset'], self.record['compressed']))
        except Exception as exc:
            raise OSError('S3 回放归档范围读取失败') from exc
        self._decoder = zlib.decompressobj(31)
        self._digest = hashlib.sha256()
        self._received = 0
        self._decoded = 0
        self._pending = b''
        self._buffer = bytearray()
        self._buffer_start = 0
        self._position = 0
        self.verified = False

    def _finish(self) -> None:
        if (self._decoder.unused_data or self._received != self.record['compressed']
                or self._decoded != self.source.size
                or self._digest.hexdigest() != self.record['sha256'].lower()):
            raise OSError('回放归档成员长度或 SHA256 校验失败')
        assert self._blocks is not None
        try:
            next(self._blocks)
        except StopIteration:
            pass
        except Exception as exc:
            raise OSError('S3 回放归档范围读取失败') from exc
        else:
            raise OSError('回放归档返回超出声明的范围')
        self.verified = True
        self._close_blocks()

    def _decode_chunk(self) -> None:
        while not self.verified:
            self._check_open()
            if not self._pending:
                if self._blocks is None:
                    raise OSError('回放归档流未完整读取')
                try:
                    self._pending = next(self._blocks)
                except StopIteration as exc:
                    raise OSError('回放归档成员被截断') from exc
                except Exception as exc:
                    raise OSError('S3 回放归档范围读取失败') from exc
                self._check_open()
                self._received += len(self._pending)
                if not self._pending or self._received > self.record['compressed']:
                    raise OSError('回放归档返回无效范围')
            try:
                chunk = self._decoder.decompress(self._pending, DECODE_CHUNK_BYTES)
            except zlib.error as exc:
                raise OSError('回放归档 gzip 或 CRC 校验失败') from exc
            self._pending = self._decoder.unconsumed_tail
            self._decoded += len(chunk)
            if self._decoded > self.source.size:
                raise OSError('回放归档解码长度超出声明')
            self._digest.update(chunk)
            self._buffer.extend(chunk)
            if self._decoder.eof:
                self._finish()
            if chunk:
                return

    def tell(self) -> int:
        self._check_open()
        return self._position

    def read(self, size: int = -1) -> bytes:
        self._check_open()
        if size == 0:
            return b''
        size = DECODE_CHUNK_BYTES if size < 0 else min(size, DECODE_CHUNK_BYTES)
        result = bytearray()
        while len(result) < size:
            if self._position == self._decoded and not self.verified:
                self._decode_chunk()
            self._check_open()
            count = min(size - len(result), self._decoded - self._position)
            if not count:
                break
            start = self._position - self._buffer_start
            result.extend(self._buffer[start:start + count])
            self._position += count
            keep_from = max(self._buffer_start, self._position - REWIND_BYTES)
            if keep_from > self._buffer_start:
                del self._buffer[:keep_from - self._buffer_start]
                self._buffer_start = keep_from
        return bytes(result)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._check_open()
        if whence == io.SEEK_CUR:
            offset += self._position
        elif whence == io.SEEK_END:
            offset += self.source.size
        elif whence != io.SEEK_SET:
            raise ValueError('不支持的回放定位方式')
        if offset < 0 or offset > self.source.size:
            raise ValueError('回放定位超出源范围')
        if offset < self._buffer_start:
            self._restart()
        if offset <= self._decoded:
            self._position = offset
        else:
            while self._position < offset:
                if not self.read(min(DECODE_CHUNK_BYTES, offset - self._position)):
                    raise OSError('回放归档定位前被截断')
        # At the exact logical EOF there may still be an outer gzip footer.
        if offset == self.source.size and not self.verified:
            self._decode_chunk()
        return self._position

    def close(self) -> None:
        self.closed = True
        self._close_blocks()
        self._buffer.clear()
        self._pending = b''


class ReplaySources:
    def __init__(self, ledger: Path, archive: Any = None) -> None:
        self.ledger = Path(ledger).resolve()
        self._archive = archive
        self._initialized = archive is not None
        self._readers: OrderedDict[tuple[Any, ...], _RemoteReplayReader] = OrderedDict()
        self._closed = False
        self._cache_lock = threading.Lock()

    def _get_archive(self) -> Any:
        if self._closed:
            raise ValueError('回放源已关闭')
        if not self._initialized:
            # archive_for checks local mode before opening credentials/DBs.
            try:
                archive = archive_for(self.ledger)
            except Exception as exc:
                raise OSError('S3 回放源初始化失败') from exc
            with self._cache_lock:
                if self._closed:
                    raise ValueError('回放源已关闭')
                self._archive = archive
                self._initialized = True
        return self._archive

    def _remote_sources(self) -> list[ReplaySource]:
        archive = self._get_archive()
        if archive is None:
            return []
        prefix = self.ledger.relative_to(Path(archive.root).resolve()).as_posix().rstrip('/') + '/'
        sources = []
        try:
            with closing(archive._db()) as db:
                rows = db.execute(
                    'SELECT path,object_key,"offset",compressed,size,sha256,mtime_ns '
                    'FROM files WHERE path>=? AND path<? ORDER BY path', (prefix, prefix + '\uffff'))
                records = [dict(zip(_RECORD_COLUMNS, row)) for row in rows]
        except Exception as exc:
            raise OSError('S3 回放源目录读取失败') from exc
        for record in records:
            relative = Path(record['path'])
            if relative.is_absolute() or '..' in relative.parts:
                raise OSError('回放归档路径无效')
            path = (Path(archive.root) / relative).resolve()
            if (path.parent != self.ledger or not path.name.startswith('live-replay')
                    or not path.name.endswith('.jsonl.gz')):
                continue
            sources.append(ReplaySource(path, int(record['size']), int(record['mtime_ns']),
                                        True, record))
        return sources

    def list_sources(self) -> list[ReplaySource]:
        sources = {source.name: source for source in self._remote_sources()}
        for path in self.ledger.glob('live-replay*.jsonl.gz'):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            if path.is_file() and not path.is_symlink():
                sources[path.name] = ReplaySource(path, stat.st_size, stat.st_mtime_ns)
        return sorted(sources.values(), key=lambda source: (source.mtime_ns, source.name), reverse=True)

    def probe_source(self) -> ReplaySource | None:
        sources = self._remote_sources()
        return max(sources, key=lambda source: (source.mtime_ns, source.name), default=None)

    @staticmethod
    def _fingerprint(source: ReplaySource) -> tuple[Any, ...]:
        record = source.record or {}
        return (source.path, record.get('object_key'), record.get('offset'), record.get('compressed'),
                record.get('sha256'), source.size, source.mtime_ns)

    @contextmanager
    def open(self, source: ReplaySource, offset: int = 0) -> Iterator[Any]:
        if self._closed:
            raise ValueError('回放源已关闭')
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= source.size:
            raise ValueError('回放定位超出源范围')
        if not source.remote:
            try:
                local = source.path.open('rb')
            except FileNotFoundError:
                remote = next((item for item in self._remote_sources()
                               if item.name == source.name and item.size == source.size
                               and item.mtime_ns == source.mtime_ns), None)
                if remote is None:
                    raise OSError('本地回放源已变化且无匹配的远端版本') from None
                with self.open(remote, offset) as stream:
                    yield stream
            else:
                with local:
                    local.seek(offset)
                    yield local
            return
        archive = self._get_archive()
        if archive is None:
            raise OSError('远端回放后端不可用')
        fingerprint = self._fingerprint(source)
        with self._cache_lock:
            if self._closed:
                raise ValueError('回放源已关闭')
            # Do not keep an older revision of the same logical source alive.
            for key in list(self._readers):
                if key[0] == source.path and key != fingerprint:
                    self._readers.pop(key).close()
            reader = self._readers.pop(fingerprint, None)
            if reader is None or reader.closed:
                reader = _RemoteReplayReader(archive, source)
            self._readers[fingerprint] = reader
            while len(self._readers) > MAX_FORWARD_READERS:
                self._readers.popitem(last=False)[1].close()
        try:
            reader.seek(offset)
            yield reader
        except BaseException:
            with self._cache_lock:
                self._readers.pop(fingerprint, None)
            reader.close()
            raise

    def close(self) -> None:
        with self._cache_lock:
            self._closed = True
            readers, self._readers = self._readers, OrderedDict()
        for reader in readers.values():
            reader.close()
