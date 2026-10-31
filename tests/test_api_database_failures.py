"""Temporary PostgreSQL proves transient HTTP errors without production writes."""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit
import uuid

import asyncpg
from fastapi import FastAPI, HTTPException
import httpx

from app import database, main
from app.routes import telegram


def http_app():
    app = FastAPI(exception_handlers=dict(main.app.exception_handlers))
    app.middleware('http')(main.request_context)
    return app


class DatabaseFailureHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_transient_database_errors_return_503_without_exception_text(self):
        failures = (asyncpg.LockNotAvailableError, asyncpg.QueryCanceledError,
                    asyncpg.DeadlockDetectedError, asyncpg.SerializationError,
                    asyncpg.TooManyConnectionsError, asyncpg.CannotConnectNowError,
                    asyncpg.ConnectionDoesNotExistError)
        for error_type in failures:
            with self.subTest(category=error_type.__name__):
                app = http_app()
                @app.post('/internal/test/write')
                async def fail():
                    raise error_type('password=secret-value host=private-db.internal')
                with patch('app.main.log.warning') as logged:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test') as client:
                        response = await client.post('/internal/test/write', headers={'X-Request-ID':'safe-db-test'})
                self.assertEqual(503,response.status_code,response.text)
                self.assertEqual('2',response.headers['Retry-After'])
                self.assertEqual('no-store',response.headers['Cache-Control'])
                self.assertEqual('safe-db-test',response.headers['X-Request-ID'])
                self.assertEqual('nosniff',response.headers['X-Content-Type-Options'])
                self.assertEqual('unconfirmed',response.json()['operation_status'])
                self.assertTrue(response.json()['retryable'])
                self.assertFalse(response.json()['automatic_retry'])
                rendered = response.text + ' '.join(str(value) for call in logged.call_args_list for value in call.args)
                self.assertNotIn('secret-value',rendered)
                self.assertNotIn('private-db.internal',rendered)

    async def test_unrelated_timeout_and_database_integrity_error_do_not_become_retryable(self):
        for error in (TimeoutError('private-timeout'),asyncpg.UniqueViolationError('private-constraint')):
            with self.subTest(category=type(error).__name__):
                app = http_app()
                @app.get('/api/test/failure')
                async def fail():
                    raise error
                with patch('app.main.log.error'):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://test') as client:
                        response = await client.get('/api/test/failure')
                self.assertEqual(500,response.status_code)
                self.assertNotIn('Retry-After',response.headers)
                self.assertNotIn('retryable',response.json())
                self.assertNotIn('private-',response.text)

    async def test_existing_http_failures_keep_original_status_and_do_not_replay(self):
        app = http_app()
        calls = []
        @app.post('/internal/test/write')
        async def fail():
            calls.append(1)
            raise HTTPException(409,'existing state conflict')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response = await client.post('/internal/test/write')
        self.assertEqual(409,response.status_code)
        self.assertEqual([1],calls)
        self.assertNotIn('Retry-After',response.headers)


ADMIN_URL = os.environ.get('TEST_DATABASE_ADMIN_URL','')


@unittest.skipUnless(ADMIN_URL,'requires explicit task-owned temporary PostgreSQL')
class DatabaseFailurePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parts = urlsplit(ADMIN_URL)
        if parts.hostname not in {'pgtest','ai-football-v2-pgverify'} or parts.username != 'postgres' or parts.path != '/postgres':
            raise RuntimeError('database failure tests only allow the isolated Docker administrator')
        self.name = 'test_db_failures_' + uuid.uuid4().hex
        self.control = await asyncpg.connect(ADMIN_URL)
        await self.control.execute('CREATE DATABASE ' + self.name)
        self.url = urlunsplit((parts.scheme,parts.netloc,'/'+self.name,parts.query,''))
        async def codecs(conn):
            for kind in ('json','jsonb'):
                await conn.set_type_codec(kind,schema='pg_catalog',encoder=json.dumps,decoder=json.loads)
        self.pool = await asyncpg.create_pool(self.url,min_size=1,max_size=3,init=codecs,command_timeout=2,
            server_settings={'lock_timeout':'75','statement_timeout':'1000'})
        async with self.pool.acquire() as conn:
            await database.apply_migrations(conn)
        self.old_pool = database.pool
        database.pool = self.pool
        self.app = http_app()
        self.app.state.settings = SimpleNamespace(collector_token='isolated-collector')
        self.app.include_router(telegram.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app,raise_app_exceptions=False),
            base_url='http://test',headers={'Authorization':'Bearer isolated-collector','X-Request-ID':'isolated-lock-test'})
        self.source,self.expert,self.owner = uuid.uuid4(),uuid.uuid4(),uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'-1001','测试来源')",self.source)
        await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'7','测试专家')",self.expert)
        await self.pool.execute('INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)',self.source,self.expert)
        self.claim = {'chat_id':'-1001','owner':str(self.owner)}
        response = await self.client.post('/internal/telegram/checkpoints/claim',json=self.claim)
        self.assertEqual(200,response.status_code,response.text)

    async def asyncTearDown(self):
        await self.client.aclose()
        database.pool = self.old_pool
        await self.pool.close()
        await self.control.execute('DROP DATABASE ' + self.name + ' WITH (FORCE)')
        await self.control.close()

    async def test_checkpoint_row_lock_timeout_returns_503_and_next_claim_recovers(self):
        before = dict(await self.pool.fetchrow('SELECT * FROM v2_source_checkpoints WHERE source_id=$1',self.source))
        async with self.pool.acquire() as blocker:
            async with blocker.transaction():
                await blocker.execute('SELECT 1 FROM v2_source_checkpoints WHERE source_id=$1 FOR UPDATE',self.source)
                response = await self.client.post('/internal/telegram/checkpoints/claim',json=self.claim)
                self.assertEqual(503,response.status_code,response.text)
                self.assertEqual('2',response.headers['Retry-After'])
                self.assertEqual('unconfirmed',response.json()['operation_status'])
        self.assertEqual(before,dict(await self.pool.fetchrow('SELECT * FROM v2_source_checkpoints WHERE source_id=$1',self.source)))
        recovered = await self.client.post('/internal/telegram/checkpoints/claim',json=self.claim)
        self.assertEqual(200,recovered.status_code,recovered.text)
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_source_checkpoints'))

    async def test_expert_row_lock_rolls_back_message_and_checkpoint_lease_then_recovers_once(self):
        before = dict(await self.pool.fetchrow('SELECT * FROM v2_source_checkpoints WHERE source_id=$1',self.source))
        now = datetime.now(timezone.utc).isoformat()
        event = {**self.claim,'platform_message_id':'11','member_message_ids':[11],
            'version_key':hashlib.sha256(b'isolated-message-v1').hexdigest(),'event_kind':'message',
            'sender_id':'7','sender_nickname':'更新昵称','source_version_at':now,'posted_at':now,
            'raw_text':'赛前大2.5','media_state':'ready','media_manifest':[],'media_objects':[]}
        async with self.pool.acquire() as blocker:
            async with blocker.transaction():
                await blocker.execute('SELECT 1 FROM v2_experts WHERE id=$1 FOR UPDATE',self.expert)
                response = await self.client.post('/internal/telegram/events',json=event)
                self.assertEqual(503,response.status_code,response.text)
        self.assertEqual(before,dict(await self.pool.fetchrow('SELECT * FROM v2_source_checkpoints WHERE source_id=$1',self.source)))
        for table in ('v2_expert_messages','v2_telegram_events','v2_analysis_jobs'):
            self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM '+table))
        recovered = await self.client.post('/internal/telegram/events',json=event)
        self.assertEqual(200,recovered.status_code,recovered.text)
        replay = await self.client.post('/internal/telegram/events',json=event)
        self.assertEqual(200,replay.status_code,replay.text)
        self.assertEqual(recovered.json()['message_id'],replay.json()['message_id'])
        for table in ('v2_expert_messages','v2_telegram_events','v2_analysis_jobs'):
            self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM '+table))
        self.assertEqual('更新昵称',await self.pool.fetchval('SELECT display_nickname FROM v2_experts WHERE id=$1',self.expert))

    async def test_unchanged_nickname_does_not_wait_for_nonkey_expert_update_lock(self):
        now = datetime.now(timezone.utc).isoformat()
        event = {**self.claim,'platform_message_id':'12','member_message_ids':[12],
            'version_key':hashlib.sha256(b'unchanged-nickname-v1').hexdigest(),'event_kind':'message',
            'sender_id':'7','sender_nickname':'测试专家','source_version_at':now,'posted_at':now,
            'raw_text':'赛前小2.5','media_state':'ready','media_manifest':[],'media_objects':[]}
        async with self.pool.acquire() as blocker:
            async with blocker.transaction():
                # A real nickname UPDATE holds NO KEY UPDATE.  FOR UPDATE
                # would also block foreign-key KEY SHARE and test another cause.
                await blocker.execute('SELECT 1 FROM v2_experts WHERE id=$1 FOR NO KEY UPDATE',self.expert)
                response = await self.client.post('/internal/telegram/events',json=event)
                self.assertEqual(200,response.status_code,response.text)
                self.assertEqual('ready',response.json()['state'])
        for table in ('v2_expert_messages','v2_telegram_events','v2_analysis_jobs'):
            self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM '+table))
        self.assertEqual('测试专家',await self.pool.fetchval('SELECT display_nickname FROM v2_experts WHERE id=$1',self.expert))

    async def test_real_statement_timeout_is_503_and_connection_remains_usable(self):
        @self.app.get('/api/test/statement-timeout')
        async def slow_query():
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SET LOCAL statement_timeout='50ms'")
                    await conn.execute('SELECT pg_sleep(0.2)')
        response = await self.client.get('/api/test/statement-timeout')
        self.assertEqual(503,response.status_code,response.text)
        self.assertEqual('2',response.headers['Retry-After'])
        self.assertNotIn('operation_status',response.json())
        self.assertEqual(1,await self.pool.fetchval('SELECT 1'))

    async def test_real_asyncpg_command_timeout_is_503_and_never_replays_query(self):
        calls = []
        @self.app.post('/internal/test/command-timeout')
        async def slow_command():
            calls.append(1)
            async with self.pool.acquire() as conn:
                await conn.execute('SELECT pg_sleep(0.2)',timeout=0.05)
        response = await self.client.post('/internal/test/command-timeout')
        self.assertEqual(503,response.status_code,response.text)
        self.assertEqual('unconfirmed',response.json()['operation_status'])
        self.assertFalse(response.json()['automatic_retry'])
        self.assertEqual([1],calls)
        self.assertEqual(1,await self.pool.fetchval('SELECT 1'))
