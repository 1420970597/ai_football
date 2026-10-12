import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from collector.http_transport import DeadlineExpired, PersistentHTTPTransport


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    received = 0
    clients: set = set()

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.do_GET()

    def do_GET(self):
        type(self).received += 1
        type(self).clients.add(self.client_address)
        if self.path == '/slow':
            time.sleep(.12)
        self.send_response(503 if self.path == '/error' else 200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        try:
            self.wfile.write(b'{}')
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


class HTTPTransportTests(unittest.TestCase):
    def setUp(self):
        _Handler.received = 0
        _Handler.clients = set()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%d' % self.server.server_port
        self.transport = PersistentHTTPTransport()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.transport.close)

    def test_reuses_connection_and_records_actual_send(self):
        sent = []
        for _ in range(3):
            raw, _ = self.transport.request(self.url, b'{}', {}, timeout=1,
                                            deadline=time.monotonic()+1, on_sent=sent.append)
            self.assertEqual(raw, b'{}')
        self.assertEqual(len(sent), 3)
        self.assertEqual(len(_Handler.clients), 1)

    def test_expired_budget_never_sends(self):
        with self.assertRaises(DeadlineExpired):
            self.transport.request(self.url, b'{}', {}, timeout=1, deadline=time.monotonic()-1)
        self.assertEqual(_Handler.received, 0)

    def test_connect_consuming_budget_cannot_send_debit(self):
        import http.client
        original = http.client.HTTPConnection.connect
        def connect(connection):
            original(connection)
            time.sleep(.04)
        with patch('collector.http_transport.http.client.HTTPConnection.connect', connect):
            with self.assertRaises(DeadlineExpired):
                self.transport.request(self.url, b'{}', {}, timeout=1, deadline=time.monotonic()+.02)
        self.assertEqual(_Handler.received, 0)

    def test_read_timeout_does_not_replay_post(self):
        with self.assertRaises(TimeoutError):
            self.transport.request(self.url+'/slow', b'{}', {}, timeout=.04)
        self.assertEqual(_Handler.received, 1)

    def test_debit_send_deadline_does_not_truncate_valid_later_receipt(self):
        sent = []
        deadline = time.monotonic()+.06
        raw, _ = self.transport.request(self.url+'/slow', b'{}', {}, timeout=.5,
                                        deadline=deadline, deadline_for_send_only=True, on_sent=sent.append)
        self.assertEqual(raw, b'{}')
        self.assertLess(sent[0], deadline)
        self.assertGreater(time.monotonic(), deadline)
        self.assertEqual(_Handler.received, 1)

    def test_http_error_and_invalid_origin(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.transport.request(self.url+'/error', None, {}, timeout=1)
        self.assertEqual(self.transport.idle.qsize(), 0)
        with self.assertRaises(ValueError):
            self.transport.request('http:///missing-host', None, {}, timeout=1)
