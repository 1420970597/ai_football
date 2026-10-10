"""Permanent compressed JSONL history, with no rotation or expiry."""
from __future__ import annotations

import gzip
import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence


class HistoryJournal:
    """Append complete gzip members; gzip.open reads all members normally.

    Compression completes before touching disk. Failed writes roll back only
    that attempt's bytes, allowing callers to retain and retry their batch.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.written = 0
        self.last_error = ''

    def append(self, rows: Sequence[Mapping[str, Any]]) -> int:
        try:
            lines = [json.dumps(row, ensure_ascii=False, allow_nan=False,
                                separators=(',', ':')) for row in rows]
        except (TypeError, ValueError) as exc:
            self.last_error = '%s: %s' % (type(exc).__name__, exc)
            raise
        return self.append_lines(lines)

    def append_lines(self, lines: Sequence[str]) -> int:
        if not lines:
            return 0
        payload = gzip.compress(('\n'.join(lines) + '\n').encode('utf-8'), compresslevel=1, mtime=0)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open('a+b', buffering=0) as file:
                    offset = file.tell()
                    try:
                        if file.write(payload) != len(payload):
                            raise OSError('incomplete history write')
                        file.flush()
                        os.fsync(file.fileno())
                    except OSError:
                        file.seek(offset)
                        file.truncate()
                        raise
                self.written += len(lines)
                self.last_error = ''
                return len(lines)
            except OSError as exc:
                self.last_error = '%s: %s' % (type(exc).__name__, exc)
                raise

    def health(self) -> dict[str, Any]:
        return {'path': str(self.path), 'retention': 'permanent',
                'written': self.written, 'last_error': self.last_error}
