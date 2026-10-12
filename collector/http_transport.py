"""Bounded reusable HTTP/1.1 connections with an absolute operation deadline."""
from __future__ import annotations

import http.client
import queue
import ssl
import time
import urllib.error
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit


class DeadlineExpired(TimeoutError):
    """No new request may be sent after its original input's deadline."""


class PersistentHTTPTransport:
    def __init__(self, *, context: ssl.SSLContext | None = None) -> None:
        self.context = context or ssl.create_default_context()
        self.idle: queue.LifoQueue = queue.LifoQueue(maxsize=12)

    @staticmethod
    def remaining(timeout: float, deadline: float | None) -> float:
        value = min(timeout, deadline-time.monotonic()) if deadline is not None else timeout
        if value <= 0:
            raise DeadlineExpired('请求的总体时间预算已耗尽')
        return value

    def request(self, url: str, body: bytes | None, headers: Mapping[str, str], *,
                timeout: float, deadline: float | None = None,
                on_sent: Callable[[float], None] | None = None,
                deadline_for_send_only: bool = False) -> tuple[bytes, Any]:
        parsed = urlsplit(url)
        if not parsed.hostname:
            raise ValueError('业务接口缺少主机名')
        origin = (parsed.scheme, parsed.hostname, parsed.port)
        connection = None
        try:
            old_origin, candidate = self.idle.get_nowait()
            if old_origin == origin:
                connection = candidate
            else:
                candidate.close()
        except queue.Empty:
            pass
        if connection is None:
            if parsed.scheme == 'https':
                connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, context=self.context)
            elif parsed.scheme == 'http':
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port)
            else:
                raise ValueError('业务接口必须使用 HTTP 或 HTTPS')
        reusable = False
        try:
            connection.timeout = self.remaining(timeout, deadline)
            if connection.sock is None:
                connection.connect()
            # Connect/DNS/TLS may consume the budget. Check again before bytes
            # are sent, including for a debit request on a new connection.
            if connection.sock is None:
                raise OSError('连接未建立')
            connection.sock.settimeout(self.remaining(timeout, deadline))
            target = parsed.path or '/'
            if parsed.query:
                target += '?' + parsed.query
            connection.request('POST' if body is not None else 'GET', target, body=body, headers=dict(headers))
            if on_sent:
                on_sent(time.monotonic())
            response_deadline = None if deadline_for_send_only else deadline
            connection.sock.settimeout(self.remaining(timeout, response_deadline))
            response = connection.getresponse()
            chunks = []
            while True:
                if connection.sock is not None:
                    connection.sock.settimeout(self.remaining(timeout, response_deadline))
                block = response.read1(65536)
                if not block:
                    break
                chunks.append(block)
            raw, response_headers = b''.join(chunks), response.headers
            if response.status >= 400:
                raise urllib.error.HTTPError(url, response.status, response.reason, response_headers, None)
            reusable = not response.will_close and connection.sock is not None
            return raw, response_headers
        finally:
            if reusable:
                try:
                    self.idle.put_nowait((origin, connection))
                except queue.Full:
                    connection.close()
            else:
                connection.close()

    def close(self) -> None:
        while True:
            try:
                _, connection = self.idle.get_nowait()
                connection.close()
            except queue.Empty:
                return
