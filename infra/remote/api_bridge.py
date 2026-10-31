"""Bounded authenticated forwarding over the project SSH Unix socket; no retries."""
from __future__ import annotations
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import socket
import threading
import time
from urllib.parse import urlsplit

SOCKET = '/run/remote/api.sock'
MAX_BODY = 4 * 1024 * 1024
UUID = r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'
COLLECTOR_ROUTES = {
    'GET': (r'/internal/collector/(config|renewal/config)', r'/internal/telegram/(sources|pending)'),
    'POST': (r'/internal/collector/(fixtures|results|renewal/claim)', rf'/internal/collector/renewal/{UUID}/finish',
             r'/internal/collector/results/(claim|observations|finish)',
             r'/internal/telegram/(checkpoints/claim|events|observed|observed/ack|media/orphans|media/orphans/ack)'),
    'PUT': (r'/internal/collector/renewal/config', r'/internal/telegram/(directory|checkpoints|session|author-backfill)'),
}
EXECUTOR_ROUTES = {
    'POST': (r'/internal/executor/(claim|dispatch)', rf'/internal/executor/jobs/{UUID}/(finish|renewal/claim|renewal/finish)',
             rf'/internal/executor/orders/{UUID}/(authorize|finish)'),
    'PUT': (rf'/internal/executor/jobs/{UUID}/session',),
}


def permitted(method: str, target: str, authorization: str, tokens: dict[str, str]) -> bool:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.fragment or '%' in target or '..' in target:
        return False
    allowed_query = (method == 'GET' and parsed.path == '/internal/telegram/pending'
                     and re.fullmatch(r'limit=([1-9]|[1-9][0-9]|100)', parsed.query))
    # The remote deployment is deliberately non-trading. The actual executor
    # sends this parameter even when its local live-orders switch is false.
    allowed_query = allowed_query or (method == 'POST' and parsed.path == '/internal/executor/claim'
                                      and parsed.query == 'live_capable=false')
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
    def __init__(self, address, tokens, socket_path=SOCKET):
        self.tokens, self.socket_path = tokens, socket_path
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass
    def setup(self):
        super().setup()
        self.connection.settimeout(10)
    def reply(self, status, body=b'{}'):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        self.forward()
    def do_POST(self):
        self.forward()
    def do_PUT(self):
        self.forward()
    def forward(self):
        if not permitted(self.command, self.path, self.headers.get('Authorization', ''), self.server.tokens):
            return self.reply(403)
        if not self.server.slots.acquire(blocking=False):
            return self.reply(503)
        upstream = None
        try:
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) > 1:
                return self.reply(400)
            raw = self.headers.get('Content-Length', '0')
            if not raw.isdecimal() or int(raw) > MAX_BODY:
                return self.reply(413)
            started = time.monotonic()
            body = self.rfile.read(int(raw))
            if len(body) != int(raw) or time.monotonic() - started > 10:
                return self.reply(408)
            upstream = UnixHTTPConnection(self.server.socket_path, timeout=max(.1, 10-(time.monotonic()-started)))
            upstream.request(self.command, self.path, body=body, headers={
                'Authorization': self.headers.get('Authorization', ''), 'Content-Type': 'application/json',
                'Content-Length': str(len(body))})
            response = upstream.getresponse()
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                return self.reply(502)
            self.reply(response.status, data)
        except (OSError, TimeoutError, http.client.HTTPException):
            self.reply(503)
        finally:
            if upstream:
                upstream.close()
            self.server.slots.release()


if __name__ == '__main__':
    tokens = {role: os.environ.get(key, '') for role, key in
              (('collector', 'COLLECTOR_INTERNAL_TOKEN'), ('executor', 'EXECUTOR_INTERNAL_TOKEN'))}
    if not all(tokens.values()) or tokens['collector'] == tokens['executor']:
        raise SystemExit('remote_bridge_credentials_invalid')
    Bridge(('0.0.0.0', 8081), tokens).serve_forever()
