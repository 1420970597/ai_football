import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

import asyncpg
from fastapi import FastAPI
import httpx

from app import database
from app.routes import operations


class CollectorHealthTests(unittest.IsolatedAsyncioTestCase):
    class _Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        response = None

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return self.response

    async def test_probe_returns_bounded_source_state_and_never_account_details(self):
        payload = {
            "connected": True,
            "authorized": True,
            "auth": {"state": "authorized", "account": {"id": "private"}},
            "source_health": {
                "leyu": {"state": "unavailable", "last_error": "websocket_http_403", "fixtures": 3},
                "websocket": {"state": "backoff", "reconnects": 2},
                "future": {"secret": "must-not-leak"},
            },
        }
        self._Client.response = self._Response(200, payload)
        request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {"settings": type("Settings", (), {"collector_token": "token"})()})()})()})()
        with patch.object(operations.httpx, "AsyncClient", self._Client):
            result = await operations.collector_health(request)
        self.assertEqual("reachable", result["state"])
        self.assertEqual("websocket_http_403", result["sources"]["leyu"]["last_error"])
        self.assertNotIn("account", json.dumps(result))
        self.assertNotIn("future", result["sources"])

    async def test_probe_classifies_missing_token_and_http_rejection(self):
        request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {"settings": type("Settings", (), {"collector_token": ""})()})()})()})()
        self.assertEqual({"state": "unconfigured", "reason": "collector_token_missing"}, await operations.collector_health(request))
        request.app.state.settings.collector_token = "token"
        self._Client.response = self._Response(403, {})
        with patch.object(operations.httpx, "AsyncClient", self._Client):
            self.assertEqual({"state": "rejected", "reason": "http_403"}, await operations.collector_health(request))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires task-owned temporary database')
class OperationsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dsn = os.environ['TEST_DATABASE_ADMIN_URL']
        self.admin = await asyncpg.connect(self.dsn)
        self.name = 'test_operations_' + uuid.uuid4().hex
        await self.admin.execute('CREATE DATABASE '+self.name)
        async def init(conn):
            await conn.set_type_codec('jsonb', schema='pg_catalog', encoder=json.dumps, decoder=json.loads)
        self.pool = await asyncpg.create_pool(self.dsn.rsplit('/',1)[0]+'/'+self.name, init=init)
        async with self.pool.acquire() as conn:
            await database.apply_migrations(conn)
        self.previous = database.pool
        database.pool = self.pool
        self.app = FastAPI()
        self.app.state.settings = SimpleNamespace(session_secret=b'k'*32, quote_max_age_seconds=15)
        self.app.state.hermes_probe = SimpleNamespace(check=AsyncMock(return_value='unavailable'))
        self.app.include_router(operations.router)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://isolated.invalid')
        self.actor = {'id':uuid.uuid4(),'role':'admin'}

    async def asyncTearDown(self):
        await self.http.aclose()
        database.pool = self.previous
        await self.pool.close()
        await self.admin.execute('DROP DATABASE '+self.name+' WITH (FORCE)')
        await self.admin.close()

    async def test_diagnostics_reports_missing_dependencies_without_claiming_health(self):
        with patch.object(operations,'require_admin',AsyncMock(return_value=self.actor)), patch.dict(os.environ,{},clear=True):
            response = await self.http.get('/api/v1/admin/diagnostics')
        self.assertEqual(200,response.status_code,response.text)
        result = response.json()
        self.assertEqual('verified',result['schema'])
        self.assertEqual('unconfigured',result['storage'])
        self.assertEqual({'gateway':'unavailable','provider':'unverified'},result['hermes'])
        self.assertIsNone(result['freshness']['quote_source_at'])
        self.assertIsNone(result['freshness']['telegram_checkpoint_at'])
        self.assertIsNone(result['freshness']['telegram_high_water'])
        self.assertEqual(0,result['unknown_orders']['count'])

    async def test_anonymous_is_rejected_and_order_access_is_read_only(self):
        self.assertEqual(401,(await self.http.get('/api/v1/admin/orders')).status_code)
        with patch.object(operations,'require_admin',AsyncMock(return_value=self.actor)):
            response = await self.http.get('/api/v1/admin/orders')
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual([],response.json()['items'])
            self.assertTrue(response.json()['read_only'])
            self.assertEqual(405,(await self.http.post('/api/v1/admin/orders')).status_code)

    async def test_real_order_rows_exclude_secrets_and_cursor_is_bound_to_filters(self):
        user, account, source, expert, message = [uuid.uuid4() for _ in range(5)]
        await self.pool.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,'member@isolated.invalid','用户','secret-password-hash','user')", user)
        await self.pool.execute("INSERT INTO v2_leyu_accounts(id,user_id,account_label,encrypted_credentials) VALUES($1,$2,'本人账户','encrypted-account-secret')",account,user)
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'isolated','测试')",source)
        await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'isolated','专家')",expert)
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,posted_at) VALUES($1,$2,$3,'1',now())",message,source,expert)
        await self.pool.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase,source_updated_at) VALUES('isolated','isolated','football','测试','甲','乙',now(),'live',now())")
        for line in (2,3):
            signal, order = uuid.uuid4(), uuid.uuid4()
            await self.pool.execute("INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,line,selection,recommendation_price,quote_time,confidence) "
                                    "VALUES($1,$2,$3,'isolated','live','total','大小',$4,'over',2,now(),0.8)",signal,message,expert,line)
            await self.pool.execute("INSERT INTO v2_bet_orders(id,user_id,signal_id,idempotency_key,state,stake,price,account_id,reserved_stake) VALUES($1,$2,$3,$4,'unknown',2,2,$5,2)",order,user,signal,str(order),account)
        with patch.object(operations,'require_admin',AsyncMock(return_value=self.actor)):
            response = await self.http.get('/api/v1/admin/orders',params={'limit':1,'state':'unknown'})
            self.assertEqual(200,response.status_code,response.text)
            self.assertNotIn('encrypted-account-secret',response.text)
            self.assertNotIn('secret-password-hash',response.text)
            self.assertEqual('本人账户',response.json()['items'][0]['account_label'])
            cursor = response.json()['next_cursor']
            self.assertTrue(cursor)
            self.assertEqual(400,(await self.http.get('/api/v1/admin/orders',params={'state':'settled','cursor':cursor})).status_code)

    def test_count_limit_is_explicit(self):
        self.assertEqual({'count':5000,'capped':True},operations.count_state(5001))
        self.assertEqual({'count':0,'capped':False},operations.count_state(None))
