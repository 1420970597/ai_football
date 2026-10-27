"""Explicit synthetic executor for isolated browser tests, never a provider adapter."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
import uuid
from urllib.parse import urlsplit

import asyncpg
import httpx

DSN = os.environ['DATABASE_URL']
parts = urlsplit(DSN)
if parts.hostname != 'pg' or parts.path != '/web_integration' or parts.query:
    raise RuntimeError('synthetic browser fixture requires its temporary database')
API = 'http://api:8080/internal/executor'
HEADERS = {'Authorization': 'Bearer integration-executor-disabled'}
EMAIL = 'extended@web-integration.invalid'
evidence = {'kind': 'synthetic_no_provider', 'verified_jobs': 0, 'refresh_jobs': 0,
            'receipts': 0, 'transaction_calls': 0, 'failures': []}
stop = threading.Event()


async def control(action):
    conn = await asyncpg.connect(DSN)
    try:
        user = await conn.fetchrow('SELECT id FROM v2_users WHERE email=$1', EMAIL)
        if not user:
            raise ValueError('isolated user does not exist')
        if action == 'age-user-session':
            await conn.execute("UPDATE v2_sessions SET reauthenticated_at=now()-interval '10 minutes' WHERE user_id=$1 AND session_kind='user'", user['id'])
            return {'aged': True}
        if action != 'seed-receipts':
            raise ValueError('unknown synthetic action')
        account = await conn.fetchrow("SELECT * FROM v2_leyu_accounts WHERE user_id=$1 AND health_state='healthy'", user['id'])
        signal = await conn.fetchval("SELECT id FROM v2_expert_signals WHERE fixture_id='web-integration-match-0' LIMIT 1")
        if not account or not signal:
            raise ValueError('synthetic account and source signal required')
        order = uuid.uuid4()
        await conn.execute("INSERT INTO v2_bet_orders(id,user_id,signal_id,idempotency_key,state,stake,price,account_id,provider_identity_hash,provider_order_no,reserved_stake) VALUES($1,$2,$3,$4,'accepted',2,1.96,$5,$6,'SYNTHETIC-BROWSER-RECEIPT',2)",
                           order,user['id'],signal,'synthetic-receipt-'+str(order),account['id'],account['provider_identity_hash'])
        return {'fixture_order_id': str(order), 'synthetic': True}
    finally:
        await conn.close()


def worker():
    with httpx.Client(timeout=5, trust_env=False, headers=HEADERS) as http:
        while not stop.wait(.3):
            try:
                claim = http.post(API + '/claim')  # No live_capable: this worker cannot claim order sends.
                if claim.status_code >= 500:
                    continue
                claim.raise_for_status()
                task = claim.json()
                if task.get('kind') != 'account':
                    continue
                job, attempt = task['job_id'], task['attempt_id']
                scope, config = task['scope'], task['config']
                if not config.get('leyu_request_id'):
                    identity = hashlib.sha256(('2001:' + config['leyu_app_login_name'].casefold()).encode()).hexdigest()
                    renewal = http.post(API + f'/jobs/{job}/renewal/claim', json={
                        'attempt_id': attempt, 'expected_revision': scope['revision'], 'identity_hash': identity})
                    renewal.raise_for_status()
                    renewal_id = renewal.json()['attempt_id']
                    persisted = http.put(API + f'/jobs/{job}/session', json={
                        'attempt_id': attempt, 'expected_revision': scope['revision'], 'renewal_attempt': renewal_id,
                        'updates': {'leyu_request_id': 'synthetic-private-request', 'leyu_api_base': 'https://synthetic.invalid'}})
                    persisted.raise_for_status()
                    finished = http.post(API + f'/jobs/{job}/renewal/finish', json={
                        'attempt_id': attempt, 'renewal_attempt': renewal_id, 'status': 'success',
                        'metadata': {'category': 'synthetic_fixture', 'login_submitted': False}})
                    finished.raise_for_status()
                    evidence['verified_jobs'] += 1
                else:
                    evidence['refresh_jobs'] += 1
                now = datetime.now(timezone.utc)
                receipts = [{'order_no': item['provider_order_no'], 'state': 'settled', 'net_profit': '1.82',
                             'pnl_basis': 'provider_reported_net', 'fees': '0.10', 'price': '1.96', 'stake': '2',
                             'payout': '3.92', 'currency': 'CNY', 'placed_at': (now-timedelta(minutes=3)).isoformat(),
                             'settled_at': now.isoformat()} for item in task.get('orders', [])]
                result = http.post(API + f'/jobs/{job}/finish', json={
                    'attempt_id': attempt, 'status': 'success', 'category': 'synthetic_fixture',
                    'balance': '123.45', 'observed_at': now.isoformat(), 'receipts': receipts,
                    'scanned_orders': [item['id'] for item in task.get('orders', [])]})
                result.raise_for_status()
                evidence['receipts'] += len(receipts)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                # Preserve only error class/status; never log returned private account configuration.
                category = type(exc).__name__
                if isinstance(exc, httpx.HTTPStatusError):
                    category += ':' + str(exc.response.status_code)
                evidence['failures'].append(category)
                if len(evidence['failures']) > 20:
                    del evidence['failures'][:-20]
                stop.wait(.5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, body):
        content = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        self.reply(200 if self.path == '/evidence' else 404, evidence if self.path == '/evidence' else {})

    def do_POST(self):
        action = self.path.strip('/')
        if action not in {'seed-receipts', 'age-user-session'}:
            return self.reply(404, {})
        try:
            self.reply(200, asyncio.run(control(action)))
        except Exception as exc:
            self.reply(409, {'category': type(exc).__name__})


if __name__ == '__main__':
    threading.Thread(target=worker, daemon=True).start()
    try:
        ThreadingHTTPServer(('0.0.0.0', 8998), Handler).serve_forever()
    finally:
        stop.set()
