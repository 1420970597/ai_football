"""Bounded authenticated forwarding over the project SSH Unix socket; no retries."""
from __future__ import annotations
import hmac
import http.client
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import socket
import threading
import time
from urllib.parse import urlsplit

SOCKET = '/run/remote/api.sock'
MAX_BODY = 4 * 1024 * 1024
SLOW_REQUEST_MS = 1000
log = logging.getLogger('platform.remote_bridge')
UUID = r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'
COLLECTOR_ROUTES = {
    'GET': (r'/internal/collector/(config|renewal/config)', r'/internal/telegram/(sources|pending)'),
    'POST': (r'/internal/collector/(fixtures|results|renewal/claim)', rf'/internal/collector/renewal/{UUID}/finish',
             r'/internal/collector/results/(claim|observations|finish)',
             r'/internal/telegram/(checkpoints/claim|checkpoints/release|events|events/receipts|observed|observed/ack|media/orphans|media/orphans/ack)'),
    'PUT': (r'/internal/collector/renewal/config', r'/internal/telegram/(directory|checkpoints|session|author-backfill)'),
}
EXECUTOR_ROUTES = {
    'POST': (r'/internal/executor/(claim|dispatch)', rf'/internal/executor/jobs/{UUID}/(finish|renewal/claim|renewal/finish)',
             rf'/internal/executor/jobs/{UUID}/history/finish',
             rf'/internal/executor/orders/{UUID}/(authorize|finish)'),
    'PUT': (rf'/internal/executor/jobs/{UUID}/session',),
}


def permitted(method: str, target: str, authorization: str, tokens: dict[str, str], *, live_orders_enabled=False) -> bool:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.fragment or '%' in target or '..' in target:
        return False
    allowed_query = (method == 'GET' and parsed.path == '/internal/telegram/pending'
                     and re.fullmatch(r'limit=([1-9]|[1-9][0-9]|100)', parsed.query))
    # Default deployment remains non-trading. An explicit bridge gate permits
    # only the executor's exact live claim query; role checks still apply.
    allowed_query = allowed_query or (method == 'POST' and parsed.path == '/internal/executor/claim'
                                      and (parsed.query == 'live_capable=false' or
                                           live_orders_enabled is True and parsed.query == 'live_capable=true'))
    if parsed.query and not allowed_query:
        return False
    for role, routes in (('collector', COLLECTOR_ROUTES), ('executor', EXECUTOR_ROUTES)):
        token = tokens.get(role, '')
        if token and hmac.compare_digest(authorization.encode(), ('Bearer ' + token).encode()):
            return any(re.fullmatch(pattern, parsed.path) for pattern in routes.get(method, ()))
    return False


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 10):
        super().__init__('project-api', timeout=timeout)
        self.path = path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Bridge(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, tokens, socket_path=SOCKET, *, live_orders_enabled=False):
        self.tokens, self.socket_path = tokens, socket_path
        self.live_orders_enabled = live_orders_enabled is True
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass
    def setup(self):
        super().setup()
        self.connection.settimeout(10)
    def reply(self, status, body=b'{}', retry_after=None):
        self.response_status = status
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if (isinstance(retry_after, str) and len(retry_after) <= 32
                and re.fullmatch(r'[0-9]{1,16}(?:\.[0-9]{1,3})?', retry_after)):
            self.send_header('Retry-After', retry_after)
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        self.forward()
    def do_POST(self):
        self.forward()
    def do_PUT(self):
        self.forward()
    def forward(self):
        if not permitted(self.command, self.path, self.headers.get('Authorization', ''), self.server.tokens,
                         live_orders_enabled=self.server.live_orders_enabled):
            return self.reply(403)
        if not self.server.slots.acquire(blocking=False):
            log.info('Remote bridge request method=%s route=%s status=503 category=capacity phase=admission',
                     self.command, re.sub(UUID, ':id', urlsplit(self.path).path))
            return self.reply(503)
        upstream = None
        started = time.monotonic()
        phase = 'body'
        timings = {}
        failure = 'none'
        try:
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) > 1:
                return self.reply(400)
            raw = self.headers.get('Content-Length', '0')
            if not raw.isdecimal() or int(raw) > MAX_BODY:
                return self.reply(413)
            body = self.rfile.read(int(raw))
            timings['body_ms'] = (time.monotonic() - started) * 1000
            if len(body) != int(raw) or time.monotonic() - started > 10:
                return self.reply(408)
            upstream = UnixHTTPConnection(self.server.socket_path, timeout=max(.1, 10-(time.monotonic()-started)))
            phase = 'upstream_request'
            phase_started = time.monotonic()
            upstream.request(self.command, self.path, body=body, headers={
                'Authorization': self.headers.get('Authorization', ''), 'Content-Type': 'application/json',
                'Content-Length': str(len(body))})
            timings['request_ms'] = (time.monotonic() - phase_started) * 1000
            phase = 'upstream_headers'
            phase_started = time.monotonic()
            response = upstream.getresponse()
            timings['headers_ms'] = (time.monotonic() - phase_started) * 1000
            phase = 'upstream_body'
            phase_started = time.monotonic()
            data = response.read(MAX_BODY + 1)
            timings['read_ms'] = (time.monotonic() - phase_started) * 1000
            if len(data) > MAX_BODY:
                return self.reply(502)
            phase = 'downstream_reply'
            self.reply(response.status, data, retry_after=response.getheader('Retry-After'))
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            failure = type(exc).__name__
            self.reply(503)
        finally:
            if upstream:
                upstream.close()
            self.server.slots.release()
            elapsed_ms = (time.monotonic() - started) * 1000
            status = getattr(self, 'response_status', 0)
            if elapsed_ms >= SLOW_REQUEST_MS or status >= 500 or failure != 'none':
                # Only authorized allowlisted paths reach here; never log the
                # query, dynamic identifiers, headers, payload or exception text.
                route = re.sub(UUID, ':id', urlsplit(self.path).path)
                log.info('Remote bridge request method=%s route=%s status=%s category=%s phase=%s '
                         'elapsed_ms=%.1f body_ms=%.1f request_ms=%.1f headers_ms=%.1f read_ms=%.1f',
                         self.command, route, status, failure, phase, elapsed_ms,
                         *(timings.get(key, -1.0) for key in ('body_ms', 'request_ms', 'headers_ms', 'read_ms')))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    tokens = {role: os.environ.get(key, '') for role, key in
              (('collector', 'COLLECTOR_INTERNAL_TOKEN'), ('executor', 'EXECUTOR_INTERNAL_TOKEN'))}
    if not all(tokens.values()) or tokens['collector'] == tokens['executor']:
        raise SystemExit('remote_bridge_credentials_invalid')
    Bridge(('0.0.0.0', 8081), tokens,
           live_orders_enabled=os.environ.get('EXECUTOR_LIVE_ORDERS_ENABLED', 'false').casefold() == 'true').serve_forever()
