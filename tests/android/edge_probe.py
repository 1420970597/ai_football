"""Real HTTP boundary checks for the project Nginx in an isolated Docker network."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class ProbeAPI(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"probe":"isolated-edge-only"}')

    do_POST = do_GET

    def log_message(self, *_args):
        pass


def request(path, data=None, headers=None):
    try:
        with urlopen(Request('http://edge' + path, data=data, headers=headers or {}), timeout=8) as response:
            return response.status, response.headers, response.read()
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def verify():
    for _ in range(30):
        try:
            if request('/api/probe')[0] == 200:
                break
        except OSError:
            pass
        time.sleep(.2)
    else:
        raise AssertionError('isolated Nginx did not become ready')
    checks = []
    for path in ['/internal/private', '/mcp/tools', '/hermes/v1', '/api/../internal/private']:
        assert request(path)[0] == 404, path
        checks.append({'check': 'internal route blocked', 'path': path, 'status': 404})
    status, headers, _body = request('/api/probe', headers={'Origin': 'https://attacker.invalid'})
    assert status == 200 and not headers.get('Access-Control-Allow-Origin')
    checks.append({'check': 'foreign CORS origin not granted', 'status': status})
    status, _headers, _body = request('/api/probe', b'x' * (1024 * 1024 + 1))
    assert status == 413, status
    checks.append({'check': 'oversized request rejected', 'status': status})
    with ThreadPoolExecutor(max_workers=32) as executor:
        statuses = list(executor.map(lambda _: request('/api/probe')[0], range(180)))
    assert 429 in statuses and set(statuses) <= {200, 429}, statuses
    checks.append({'check': 'burst limit returns 429', 'accepted': statuses.count(200), 'limited': statuses.count(429)})
    print(json.dumps({'scope': 'isolated HTTP edge; no production providers', 'checks': checks}, sort_keys=True))


if __name__ == '__main__':
    if sys.argv[1:] == ['serve']:
        ThreadingHTTPServer(('0.0.0.0', 8080), ProbeAPI).serve_forever()
    else:
        verify()
