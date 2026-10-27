"""Isolated fault/recovery loops; never use production or send funded orders."""
import asyncio
from datetime import datetime, timezone
from io import BytesIO
import json
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import httpx
import asyncpg
from PIL import Image
from telethon import utils
from telethon.errors import FloodWaitError

from app import worker
from app.routes import telegram
from collector.telegram import TelegramCollector
from collector.telegram_backend import TelegramBackend
import test_api_database_failures as db_support
import test_mcp_transport as mcp_support
import test_reviews as review_support
import test_telegram_collector as tg_support


HAS_DATABASE = bool(os.environ.get('TEST_DATABASE_ADMIN_URL'))


class DatabaseInterfaceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def response(self, error):
        app = db_support.http_app()
        calls = []
        @app.post('/internal/test/masked-disconnect')
        async def fail():
            calls.append(1)
            raise error
        with patch('app.main.log.warning'), patch('app.main.log.error'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                         base_url='http://test') as client:
                response = await client.post('/internal/test/masked-disconnect')
        self.assertEqual([1], calls)
        self.assertNotIn('private-secret', response.text)
        return response

    async def test_typed_cause_and_context_disconnect_are_503_without_replaying_write(self):
        for link in ('__cause__', '__context__'):
            with self.subTest(link=link):
                error = asyncpg.InterfaceError('private-secret')
                setattr(error, link, asyncpg.ConnectionDoesNotExistError('private-secret'))
                response = await self.response(error)
                self.assertEqual(503, response.status_code, response.text)
                self.assertEqual('2', response.headers['Retry-After'])
                self.assertEqual('unconfirmed', response.json()['operation_status'])
                self.assertFalse(response.json()['automatic_retry'])

    async def test_unrelated_interface_error_cycles_and_unbounded_chain_remain_500(self):
        ordinary = asyncpg.InterfaceError('private-secret: connection is closed')
        cycle = asyncpg.InterfaceError('private-secret')
        cycle.__context__ = cycle
        deep = asyncpg.ConnectionDoesNotExistError('private-secret')
        for _ in range(8):
            outer = asyncpg.InterfaceError('private-secret')
            outer.__cause__ = deep
            deep = outer
        for error in (ordinary, cycle, deep):
            response = await self.response(error)
            self.assertEqual(500, response.status_code, response.text)
            self.assertNotIn('Retry-After', response.headers)
            self.assertNotIn('retryable', response.json())


@unittest.skipUnless(HAS_DATABASE, 'requires explicit task-owned temporary PostgreSQL')
class TelegramDatabaseRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = db_support.DatabaseFailurePostgresTests.asyncSetUp
    asyncTearDown = db_support.DatabaseFailurePostgresTests.asyncTearDown

    def event(self):
        import hashlib
        now = datetime.now(timezone.utc).isoformat()
        return {**self.claim, 'platform_message_id':'81', 'member_message_ids':[81],
                'version_key':hashlib.sha256(b'fault-recovery-81').hexdigest(),
                'event_kind':'message', 'sender_id':'7', 'sender_nickname':'测试专家',
                'source_version_at':now, 'posted_at':now, 'raw_text':'赛前大2.5',
                'media_state':'ready', 'media_manifest':[], 'media_objects':[]}

    async def test_real_connection_loss_rolls_back_then_replay_persists_exactly_once(self):
        before = dict(await self.pool.fetchrow(
            'SELECT * FROM v2_source_checkpoints WHERE source_id=$1', self.source))
        entered, backend = asyncio.Event(), {}
        original = telegram.check_lease
        calls = []
        async def interrupted_lease(conn, source_id, owner):
            result = await original(conn, source_id, owner)
            calls.append(1)
            if len(calls) == 1:
                backend['pid'] = await conn.fetchval('SELECT pg_backend_pid()')
                entered.set()
                await conn.execute('SELECT pg_sleep(10)')
            return result
        body = self.event()
        with patch.object(telegram, 'check_lease', interrupted_lease):
            pending = asyncio.create_task(self.client.post('/internal/telegram/events', json=body))
            await asyncio.wait_for(entered.wait(), 2)
            try:
                # Only terminate the connection belonging to this unique test DB,
                # and only once its real query is running on the server.
                for _ in range(100):
                    sleeping = await self.control.fetchval(
                        "SELECT wait_event='PgSleep' FROM pg_stat_activity WHERE pid=$1 AND datname=$2",
                        backend['pid'], self.name)
                    if sleeping:
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(sleeping)
                self.assertTrue(await self.control.fetchval(
                    'SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid=$1 AND datname=$2',
                    backend['pid'], self.name))
                failed = await asyncio.wait_for(pending, 3)
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
        self.assertEqual(503, failed.status_code, failed.text)
        self.assertEqual('2', failed.headers['Retry-After'])
        self.assertEqual('unconfirmed', failed.json()['operation_status'])
        self.assertFalse(failed.json()['automatic_retry'])
        self.assertEqual([1], calls)
        self.assertEqual(before, dict(await self.pool.fetchrow(
            'SELECT * FROM v2_source_checkpoints WHERE source_id=$1', self.source)))
        for table in ('v2_expert_messages', 'v2_telegram_events', 'v2_analysis_jobs'):
            self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM '+table))
        restored = await self.client.post('/internal/telegram/events', json=body)
        replay = await self.client.post('/internal/telegram/events', json=body)
        self.assertEqual(200, restored.status_code, restored.text)
        self.assertEqual(200, replay.status_code, replay.text)
        self.assertEqual(restored.json()['message_id'], replay.json()['message_id'])
        for table in ('v2_expert_messages', 'v2_telegram_events', 'v2_analysis_jobs'):
            self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM '+table))

    async def test_s3_deadline_late_completion_then_retry_enqueues_one_database_job(self):
        backend = TelegramBackend(self.client, 'http://test/internal/telegram')
        client = tg_support.Client()
        message = tg_support.Message(1, media=True, mime='image/png')
        message.date = datetime.now(timezone.utc)
        client.messages['-1001'] = [message]
        body = BytesIO(); Image.new('RGB', (8, 8), 'blue').save(body, format='PNG')
        client.download_chunks = [body.getvalue()]
        collector = TelegramCollector(tg_support.CONFIG, backend=backend, client_factory=lambda *_:client)
        collector.owner = str(self.owner)
        collector.client = client
        source = {'chat_id':'-1001', 'trusted_sender_ids':['7'], 'allow_media':True}
        collector.sources = {'-1001':source}
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        writes, objects = [], {}
        class S3:
            def put_object(inner, **kwargs):
                writes.append(kwargs['Key'])
                if len(writes) == 1:
                    entered.set()
                    release.wait(5)
                objects[kwargs['Key']] = kwargs['Body']
                finished.set()
            def close(inner):
                pass
        env = {'S3_BUCKET':'test-private', 'S3_ENDPOINT_URL':'https://invalid.example',
               'S3_ACCESS_KEY':'test-only', 'S3_SECRET_KEY':'test-only'}
        try:
            await collector._sync_source('-1001', source)
            self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_jobs'))
            with patch.dict(os.environ, env), patch('collector.telegram.boto3.client', return_value=S3()), \
                    patch('collector.telegram.MEDIA_TRANSFER_SECONDS', .25):
                try:
                    await collector.retry_pending()
                    self.assertTrue(entered.is_set())
                    row = await self.pool.fetchrow('SELECT media_state,media_objects FROM v2_expert_messages')
                    self.assertEqual('failed', row['media_state'])
                    self.assertEqual(1, len(row['media_objects']))
                    self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_jobs'))
                finally:
                    release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 1))
                self.assertEqual('failed', await self.pool.fetchval('SELECT media_state FROM v2_expert_messages'))
                with patch('collector.telegram.MEDIA_TRANSFER_SECONDS', 1):
                    await collector.retry_pending()
                    await collector.retry_pending()
            self.assertEqual(2, len(writes))
            self.assertEqual(1, len(set(writes)))
            self.assertEqual({writes[0]:body.getvalue()}, objects)
            self.assertEqual('ready', await self.pool.fetchval('SELECT media_state FROM v2_expert_messages'))
            self.assertEqual([], await backend.pending())
            for table in ('v2_expert_messages', 'v2_telegram_events', 'v2_analysis_jobs'):
                self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM '+table))
        finally:
            release.set()
            await collector.disconnect()


class TelegramFloodRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_flood_wait_deadline_resumes_once_without_blocking_other_group(self):
        backend, client = tg_support.Backend(), tg_support.Client()
        backend.source_items.append({'chat_id':'-1002', 'trusted_sender_ids':['7'], 'allow_media':True})
        client.messages = {'-1001':[tg_support.Message(1)], '-1002':[tg_support.Message(3)]}
        collector = TelegramCollector(tg_support.CONFIG, backend=backend, client_factory=lambda *_:client)
        original, requests = client.iter_messages, []
        async def messages(entity, **kwargs):
            chat = str(utils.get_peer_id(entity))
            requests.append(chat)
            if chat == '-1001' and requests.count(chat) == 1:
                raise FloodWaitError(None, capture=60)
            async for message in original(entity, **kwargs):
                yield message
        client.iter_messages = messages
        clock = SimpleNamespace(monotonic=lambda:100)
        try:
            # Replace only the collector module clock, not asyncio's clock.
            with patch('collector.telegram.time', clock):
                first = await collector.sync_once()
                self.assertEqual('flood_wait', first['sources']['-1001']['state'])
                self.assertEqual(160, collector.retry_at['-1001'])
                self.assertEqual(3, backend.positions['-1002'])
                clock.monotonic = lambda:159.999
                second = await collector.sync_once()
                self.assertEqual('backoff', second['sources']['-1001']['state'])
                self.assertEqual(1, requests.count('-1001'))
                self.assertNotIn('-1001', backend.positions)
                clock.monotonic = lambda:160
                recovered = await collector.sync_once()
                self.assertEqual('caught_up', recovered['sources']['-1001']['state'])
                self.assertEqual(1, recovered['sources']['-1001']['observed'])
                again = await collector.sync_once()
                self.assertEqual(0, again['sources']['-1001']['observed'])
            self.assertEqual({'-1001':1, '-1002':3}, backend.positions)
            self.assertEqual(2, len(backend.events))
            self.assertEqual(1, sum(v['chat_id']=='-1001' for v in backend.calls))
            self.assertEqual(1, client.connect_count)
        finally:
            await collector.disconnect()


@unittest.skipUnless(HAS_DATABASE, 'requires explicit task-owned temporary PostgreSQL')
class ReviewFaultRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parts = urlsplit(os.environ['TEST_DATABASE_ADMIN_URL'])
        if (parts.hostname not in {'pgtest', 'ai-football-v2-pgtest', 'ai-football-v2-pgverify'}
                or parts.username != 'postgres' or parts.path != '/postgres'):
            raise RuntimeError('fault recovery requires an explicitly isolated Docker administrator')
        await review_support.ReviewTests.asyncSetUp(self)

    asyncTearDown = review_support.ReviewTests.asyncTearDown
    context = review_support.ReviewTests.context
    post = review_support.ReviewTests.post
    fixture = review_support.ReviewTests.fixture
    search_all = review_support.ReviewTests.search_all
    resolve = review_support.ReviewTests.resolve
    read_market = review_support.ReviewTests.read_market

    async def test_provider_429_releases_lease_backoff_then_new_attempt_records_once(self):
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='queued',attempts=0,attempt_id=NULL,lease_until=NULL")
        first = await worker.claim_job(self.pool)
        calls = []
        async def model(settings, message, job_id, attempt_id):
            calls.append(attempt_id)
            if len(calls) == 1:
                request = httpx.Request('POST', 'https://isolated-model.invalid')
                raise httpx.HTTPStatusError('fixture throttled', request=request, response=httpx.Response(429, request=request))
            self.attempt = attempt_id
            fixture_id = await self.fixture()
            key, _ = await self.search_all()
            await self.resolve(key)
            quote = await self.read_market(fixture_id)
            response = await self.post('record_expert_signals', signals=[{
                'fixture_id':fixture_id, 'quote_id':quote['quote_id'], 'evidence_text':'赛前大2.5'}])
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(1, len(response.json()['accepted']))
            return '系统已接受', {'content':'系统已接受'}
        cleanup = AsyncMock()
        with patch.object(worker, 'review_message', model), patch.object(worker, 'delete_session', cleanup):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model='hermes-agent'), first)
            row = await self.pool.fetchrow('SELECT * FROM v2_analysis_jobs WHERE id=$1', self.job)
            self.assertEqual('retry', row['state'])
            self.assertIsNone(row['lease_until'])
            self.assertGreater(row['available_at'], datetime.now(timezone.utc))
            self.assertIsNone(await worker.claim_job(self.pool))
            self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
            trace = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='review_failure'")
            self.assertEqual('provider_transient', trace['failure_category'])
            self.assertTrue(trace['retryable'])
            # Advance the persisted retry deadline, without sleeping or changing
            # lease/attempt logic; production still observes its backoff.
            await self.pool.execute("UPDATE v2_analysis_jobs SET available_at=now()-interval '1 second' WHERE id=$1", self.job)
            second = await worker.claim_job(self.pool)
            self.assertEqual(2, second['attempts'])
            self.assertNotEqual(first['attempt_id'], second['attempt_id'])
            await worker.process_job(self.pool, SimpleNamespace(hermes_model='hermes-agent'), second)
        self.assertEqual(2, cleanup.await_count)
        self.assertEqual([first['attempt_id'], second['attempt_id']], calls)
        self.assertEqual('succeeded', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertIsNone(await worker.claim_job(self.pool))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))

    async def test_mcp_committed_record_response_loss_then_protocol_retry_is_idempotent(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        quote = await self.read_market(fixture_id)
        # Real MCP SDK -> real business API -> temporary PostgreSQL. Only the
        # first *response* after the committed business call is discarded.
        self.business_client = self.client
        self.business_app = self.client._transport.app
        await mcp_support.MCPTransportTests.asyncSetUp(self)
        await self.server.client.aclose()
        direct = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.business_app), base_url='http://test',
                                  headers={'Authorization':'Bearer isolated-tools'})
        sent = []
        async def upstream(request):
            response = await direct.post(request.url.path, json=json.loads(request.content))
            sent.append(response.status_code)
            if len(sent) == 1:
                raise httpx.ReadTimeout('isolated lost response', request=request)
            return httpx.Response(response.status_code, json=response.json())
        self.server.API_URL = 'http://test/internal/mcp'
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        args = self.context(signals=[{'fixture_id':fixture_id, 'quote_id':quote['quote_id'], 'evidence_text':'大2.5'}])
        try:
            failed = await self.rpc('tools/call', {'name':'record_expert_signals', 'arguments':args})
            self.assertTrue(failed.json()['result']['isError'])
            self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
            recovered = await self.rpc('tools/call', {'name':'record_expert_signals', 'arguments':args})
            self.assertFalse(recovered.json()['result'].get('isError', False), recovered.text)
            result = recovered.json()['result']
            accepted = result.get('structuredContent')
            if accepted is None:
                accepted = json.loads(result['content'][0]['text'])
            self.assertEqual(1, len(accepted['accepted']))
            self.assertTrue(accepted['accepted'][0]['duplicate'])
            self.assertEqual([200, 200], sent)
            self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
            self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))
        finally:
            await direct.aclose()
            await mcp_support.MCPTransportTests.asyncTearDown(self)
            self.client = self.business_client

    rpc = mcp_support.MCPTransportTests.rpc
