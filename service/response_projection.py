"""Single background publisher; browser reads never build a board or query history."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable


class ResponseProjection:
    def __init__(self, build: Callable[[str, str], dict[str, Any]], interval: float = 1.0) -> None:
        self.build = build
        self.interval = interval
        self._lock = threading.Lock()
        self._wanted: set[tuple[str, str]] = set()
        self._values: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._errors: dict[tuple[str, str], str] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='api-projection', daemon=True)
        self._thread.start()

    def read(self, kind: str, name: str, empty: dict[str, Any]) -> dict[str, Any]:
        key = (kind, name)
        with self._lock:
            first = key not in self._wanted
            self._wanted.add(key)
            value = self._values.get(key)
            error = self._errors.get(key, '')
        if first:
            self._wake.set()
        age = max(0.0, time.monotonic()-value[0]) if value else None
        # Published containers are immutable. Only add per-request metadata;
        # deep-copying thousands of rows on every browser poll defeats isolation.
        return {**(value[1] if value else empty), 'projection': {
            'loading': value is None, 'age_s': round(age, 3) if age is not None else None,
            'stale': bool(error) or age is None or age > self.interval*3,
            'error': error}}

    def refresh(self) -> None:
        with self._lock:
            keys = list(self._wanted)
        for kind, name in sorted(keys):
            if self._stop.is_set():
                return
            try:
                payload = self.build(kind, name)
                with self._lock:
                    self._values[(kind, name)] = (time.monotonic(), payload)
                    self._errors.pop((kind, name), None)
            except Exception:
                # Keep the last good snapshot and expose its age. Provider
                # response text and credentials must never enter this API.
                with self._lock:
                    self._errors[(kind, name)] = '后台数据更新失败，保留上次结果'

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._wake.wait(self.interval)
            self._wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(5)
