"""Read-only account telemetry with one background refresh per credential set."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Callable

ACCOUNT_REFRESH_S = 5.0


class AccountSummary:
    def __init__(self, client_factory: Callable[[], Any] | None = None) -> None:
        self._factory = client_factory
        self._lock = threading.Lock()
        self._value: dict[str, Any] = {}
        self._at = 0.0
        self._updated_at_ms: int | None = None
        self._refreshing = False
        self._key = ''
        self._client: Any = None
        self._error = ''
        self._thread: threading.Thread | None = None

    @staticmethod
    def _credentials_key() -> str:
        values = sorted((k, v) for k, v in os.environ.items() if k.startswith('LEYU_'))
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    def read(self) -> dict[str, Any]:
        key = self._credentials_key()
        with self._lock:
            if key != self._key:
                self._key, self._value, self._at, self._updated_at_ms = key, {}, 0.0, None
                self._client, self._error = None, ''
            age = time.monotonic() - self._at
            if not self._refreshing and (not self._at or age >= ACCOUNT_REFRESH_S):
                self._refreshing = True
                self._thread = threading.Thread(target=self._refresh, args=(key,),
                                                name='account-summary-refresh', daemon=True)
                self._thread.start()
            return {**(self._value or {'available': False, 'source': 'leyu_app', 'error': '正在读取账户数据'}),
                    'loading': self._refreshing, 'stale': bool(self._value) and age >= ACCOUNT_REFRESH_S,
                    'updated_at_ms': self._updated_at_ms, 'refresh_error': self._error}

    def _refresh(self, key: str) -> None:
        try:
            if self._factory:
                client = self._factory()
            else:
                from collector.leyu_account import account_client_from_env
                with self._lock:
                    client = self._client
                if client is None:
                    client = account_client_from_env()
            result = (client.fetch() if client is not None else
                      {'available': False, 'source': 'leyu_app', 'error': '未配置 App 会话'})
            with self._lock:
                if key == self._key:
                    self._value, self._client = result, client
                    self._updated_at_ms = int(time.time() * 1000)
                    self._error = ''
        except Exception as exc:
            # Never expose upstream exception bodies or authentication details.
            with self._lock:
                if key == self._key:
                    self._error = '账户刷新失败（%s）' % type(exc).__name__
                    if not self._value:
                        self._value = {'available': False, 'source': 'leyu_app', 'error': self._error}
        finally:
            with self._lock:
                if key == self._key:
                    self._at = time.monotonic()
                self._refreshing = False
