"""Durable reconciliation with an explicitly temporary PostgreSQL only."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

import asyncpg
import httpx

from app import database
from collector.telegram import TelegramCollector
from collector.telegram_backend import TelegramBackend
import test_telegram_api as support
import test_telegram_collector as collector_support


class ReconciliationTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_collector_explicitly_sends_exact_claim_ack_protocol_and_preserves_items(self):
        requests = []
        expected = [{'id': 'test-event-only', 'claim_id': 'test-claim-only'}]
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={'items': expected, 'claim_lease_seconds': 30})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            result = await backend.observed('-1001', 'test-owner-only', limit=10)
        self.assertEqual(expected, result)
        self.assertEqual(1, len(requests))
        self.assertEqual('POST', requests[0].method)
        self.assertEqual('/internal/telegram/observed', requests[0].url.path)
        self.assertEqual({'chat_id': '-1001', 'owner': 'test-owner-only', 'limit': 10,
                          'reconciliation_protocol': 'claim-ack-v1'}, json.loads(requests[0].content))

    async def test_protocol_rejection_is_not_retried_or_downgraded(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(422, json={'detail': 'unsupported protocol'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            with self.assertRaises(httpx.HTTPStatusError) as raised:
                await backend.observed('-1001', 'test-owner-only')
        self.assertEqual(422, raised.exception.response.status_code)
        self.assertEqual(1, len(requests))
        self.assertEqual('claim-ack-v1', json.loads(requests[0].content)['reconciliation_protocol'])


@unittest.skipUnless(support.TEST_URL, 'requires explicit temporary Telegram PostgreSQL')
class ReconciliationClaimsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.TelegramApiTests.asyncSetUp
    asyncTearDown = support.TelegramApiTests.asyncTearDown
    event = support.TelegramApiTests.event
    post = support.TelegramApiTests.post

    async def seed(self, number=11, **kwargs):
        body = self.event(**kwargs)
        body.update(platform_message_id=str(number), member_message_ids=[number])
        result = await self.post(body)
        self.assertEqual(200, result.status_code, result.text)
        await self.pool.execute("UPDATE v2_telegram_events SET next_check_at=now()-interval '1 minute' WHERE platform_message_id=$1", str(number))
        return body

    async def observe(self, **kwargs):
        result = await self.client.post('/internal/telegram/observed', json={
            'chat_id': '-1001', 'owner': str(self.owner), 'limit': 10,
            'reconciliation_protocol': 'claim-ack-v1', **kwargs})
        self.assertEqual(200, result.status_code, result.text)
        return result.json()['items']

    def reference(self, item):
        return {key: item[source] for key, source in (
            ('event_id', 'id'), ('version_key', 'version_key'), ('claim_id', 'claim_id'))}

    async def ack(self, item, **kwargs):
        return await self.client.post('/internal/telegram/observed/ack', json={
            'chat_id': '-1001', 'owner': str(self.owner), 'reconciliation': self.reference(item), **kwargs})

    async def checked(self, body, item):
        return await self.post(body | {'reconciliation': self.reference(item)})

    async def expire(self, item):
        await self.pool.execute("UPDATE v2_telegram_events SET reconcile_lease_until=now()-interval '1 second' WHERE id=$1", uuid.UUID(item['id']))

    async def test_old_missing_or_invalid_protocol_rejected_before_any_claim_or_due_change(self):
        await self.seed()
        before = await self.pool.fetchrow('SELECT * FROM v2_telegram_events')
        checkpoint = await self.pool.fetchrow('SELECT * FROM v2_source_checkpoints')
        old_body = {'chat_id': '-1001', 'owner': str(self.owner), 'limit': 10}
        for extra in ({}, {'reconciliation_protocol': 'legacy'}, {'reconciliation_protocol': 'claim-ack-v2'},
                      {'reconciliation_protocol': 'CLAIM-ACK-V1'}, {'reconciliation_protocol': None},
                      {'reconciliation_protocol': 1}, {'reconciliation_protocol': True}):
            with self.subTest(protocol=extra):
                response = await self.client.post('/internal/telegram/observed', json=old_body | extra)
                self.assertEqual(422, response.status_code, response.text)
                self.assertEqual(before, await self.pool.fetchrow('SELECT * FROM v2_telegram_events'))
                self.assertEqual(checkpoint, await self.pool.fetchrow('SELECT * FROM v2_source_checkpoints'))
        item, = await self.observe()
        self.assertIsNotNone(item['claim_id'])
        self.assertEqual(before['next_check_at'], await self.pool.fetchval('SELECT next_check_at FROM v2_telegram_events'))

    async def test_invalid_protocol_cannot_mutate_or_replace_existing_active_claim(self):
        body = await self.seed()
        item, = await self.observe()
        before = await self.pool.fetchrow('SELECT * FROM v2_telegram_events')
        checkpoint = await self.pool.fetchrow('SELECT * FROM v2_source_checkpoints')
        for extra in ({}, {'reconciliation_protocol': 'claim-ack-v0'}):
            response = await self.client.post('/internal/telegram/observed', json={
                'chat_id': '-1001', 'owner': str(self.owner), 'limit': 10, **extra})
            self.assertEqual(422, response.status_code, response.text)
            self.assertEqual(before, await self.pool.fetchrow('SELECT * FROM v2_telegram_events'))
            self.assertEqual(checkpoint, await self.pool.fetchrow('SELECT * FROM v2_source_checkpoints'))
        self.assertEqual(200, (await self.checked(body, item)).status_code)
        self.assertEqual(200, (await self.ack(item)).status_code)

    async def test_claim_keeps_due_time_until_committed_check_and_idempotent_ack(self):
        body = await self.seed()
        due = await self.pool.fetchval('SELECT next_check_at FROM v2_telegram_events')
        item, = await self.observe()
        self.assertEqual(due, await self.pool.fetchval('SELECT next_check_at FROM v2_telegram_events'))
        self.assertEqual([], await self.observe())
        self.assertEqual(409, (await self.ack(item)).status_code)
        applied = await self.checked(body, item)
        self.assertEqual(200, applied.status_code, applied.text)
        self.assertEqual(due, await self.pool.fetchval('SELECT next_check_at FROM v2_telegram_events'))
        self.assertEqual({'acknowledged': True, 'duplicate': False}, (await self.ack(item)).json())
        after = await self.pool.fetchrow('SELECT next_check_at,reconcile_acknowledged_at FROM v2_telegram_events')
        self.assertGreater(after['next_check_at'], datetime.now(timezone.utc))
        self.assertEqual({'acknowledged': True, 'duplicate': True}, (await self.ack(item)).json())
        self.assertEqual(after, await self.pool.fetchrow('SELECT next_check_at,reconcile_acknowledged_at FROM v2_telegram_events'))

    async def test_lost_claim_response_reclaims_after_short_lease_not_six_hours(self):
        body = await self.seed()
        lost, = await self.observe()
        self.assertEqual([], await self.observe())
        await self.expire(lost)
        restarted, = await self.observe()
        self.assertNotEqual(lost['claim_id'], restarted['claim_id'])
        self.assertEqual(lost['id'], restarted['id'])
        self.assertEqual(409, (await self.checked(body, lost)).status_code)
        self.assertEqual(409, (await self.ack(lost)).status_code)
        self.assertEqual(200, (await self.checked(body, restarted)).status_code)
        self.assertEqual(200, (await self.ack(restarted)).status_code)
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_messages'))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_jobs'))

    async def test_expired_claim_never_creates_deletion_or_tombstone(self):
        await self.seed()
        item, = await self.observe()
        await self.expire(item)
        deletion = self.event(kind='delete', text='', version='deleted')
        deletion.update(platform_message_id='delete:11', reconciliation=self.reference(item))
        self.assertEqual(409, (await self.post(deletion)).status_code)
        self.assertEqual(409, (await self.ack(item)).status_code)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_telegram_tombstones'))
        self.assertTrue(await self.pool.fetchval('SELECT is_current FROM v2_telegram_events'))
        self.assertIsNone(await self.pool.fetchval('SELECT deleted_at FROM v2_expert_messages'))

    async def test_owner_source_version_identity_and_scope_cannot_be_forged(self):
        body = await self.seed()
        item, = await self.observe()
        for changed in ({'owner': str(uuid.uuid4())},
                        {'reconciliation': self.reference(item) | {'version_key': '0'*64}},
                        {'reconciliation': self.reference(item) | {'event_id': str(uuid.uuid4())}},
                        {'reconciliation': self.reference(item) | {'claim_id': str(uuid.uuid4())}},
                        {'platform_message_id': '12', 'member_message_ids': [12]}):
            result = await self.post(body | {'reconciliation': self.reference(item)} | changed)
            self.assertEqual(409, result.status_code, result.text)
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'-1002','另一来源')", uuid.uuid4())
        lease = await self.client.post('/internal/telegram/checkpoints/claim', json={'chat_id': '-1002', 'owner': str(self.owner)})
        self.assertEqual(200, lease.status_code)
        self.assertEqual(409, (await self.post(body | {'chat_id': '-1002', 'reconciliation': self.reference(item)})).status_code)
        self.client.headers['Authorization'] = 'Bearer invalid'
        self.assertEqual(401, (await self.ack(item)).status_code)
        self.client.headers['Authorization'] = 'Bearer test-collector-token'
        self.assertIsNone(await self.pool.fetchval('SELECT reconcile_applied_at FROM v2_telegram_events'))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_messages'))

    async def test_newer_event_rejects_old_claim_without_overwriting_latest(self):
        old = await self.seed()
        item, = await self.observe()
        newer = self.event(version='v2', text='改为主队让0.5', kind='edit', minutes=1)
        self.assertEqual(200, (await self.post(newer)).status_code)
        self.assertEqual(409, (await self.checked(old, item)).status_code)
        self.assertEqual(409, (await self.ack(item)).status_code)
        self.assertEqual('改为主队让0.5', await self.pool.fetchval(
            'SELECT m.raw_text FROM v2_telegram_events e JOIN v2_expert_messages m ON m.id=e.message_id WHERE e.is_current'))
        self.assertEqual(2, await self.pool.fetchval('SELECT count(*) FROM v2_expert_messages'))

    async def test_atomic_edit_ack_preserves_original_new_due_and_rejects_conflicting_retry(self):
        await self.seed()
        item, = await self.observe()
        edit = self.event(version='v2', text='改为小2.5', kind='edit', minutes=1)
        result = await self.checked(edit, item)
        self.assertEqual(200, result.status_code, result.text)
        next_due = await self.pool.fetchval('SELECT next_check_at FROM v2_telegram_events WHERE is_current')
        self.assertEqual(200, (await self.ack(item)).status_code)
        self.assertEqual(next_due, await self.pool.fetchval('SELECT next_check_at FROM v2_telegram_events WHERE is_current'))
        self.assertEqual('赛前阿森纳大2.5', await self.pool.fetchval(
            'SELECT m.raw_text FROM v2_telegram_events e JOIN v2_expert_messages m ON m.id=e.message_id WHERE e.id=$1', uuid.UUID(item['id'])))
        self.assertEqual('reconciled', (await self.checked(edit, item)).json()['state'])
        self.assertEqual(409, (await self.checked(edit | {'raw_text': '冲突回包不可覆盖'}, item)).status_code)
        self.assertEqual(2, await self.pool.fetchval('SELECT count(*) FROM v2_expert_messages'))

    async def test_failed_transaction_rolls_back_application_marker(self):
        body = await self.seed()
        item, = await self.observe()
        self.assertEqual(409, (await self.checked(body | {'raw_text': '同版本冲突'}, item)).status_code)
        self.assertIsNone(await self.pool.fetchval('SELECT reconcile_applied_at FROM v2_telegram_events'))
        self.assertEqual(409, (await self.ack(item)).status_code)
        self.assertEqual(200, (await self.checked(body, item)).status_code)
        self.assertEqual(200, (await self.ack(item)).status_code)

    async def test_concurrent_claims_disjoint_and_more_than_ten_remain_due(self):
        for number in range(1,24):
            await self.seed(number, version=f'message-{number}')
        first, second = await asyncio.gather(self.observe(), self.observe())
        first_ids, second_ids = {item['id'] for item in first}, {item['id'] for item in second}
        self.assertEqual(10, len(first_ids))
        self.assertEqual(10, len(second_ids))
        self.assertFalse(first_ids & second_ids)
        rest = await self.observe()
        self.assertEqual(3,len(rest))
        self.assertEqual(23,len(first_ids | second_ids | {item['id'] for item in rest}))
        self.assertEqual([],await self.observe())
        self.assertEqual(23,await self.pool.fetchval('SELECT count(*) FROM v2_telegram_events WHERE next_check_at<=now()'))

    async def test_recent_five_minutes_and_historical_six_hours_start_at_ack(self):
        now = datetime.now(timezone.utc)
        for number, age, seconds in ((11,timedelta(hours=1),300),(12,timedelta(days=10),21600)):
            body = await self.seed(number,version=f'age-{number}')
            await self.pool.execute('UPDATE v2_telegram_events SET source_version_at=$2 WHERE platform_message_id=$1',str(number),now-age)
            item, = await self.observe()
            self.assertEqual(200,(await self.checked(body,item)).status_code)
            self.assertEqual(200,(await self.ack(item)).status_code)
            remaining = await self.pool.fetchval('SELECT EXTRACT(EPOCH FROM next_check_at-now()) FROM v2_telegram_events WHERE id=$1',uuid.UUID(item['id']))
            self.assertLess(abs(float(remaining)-seconds),3)

    async def test_collector_lost_ack_response_retries_ack_without_second_edit(self):
        await self.seed()
        backend = TelegramBackend(self.client,'http://testserver/internal/telegram')
        client = collector_support.Client({'-1001':[collector_support.Message(11)]})
        client.messages['-1001'][0].edit_date = datetime(2026,10,26,0,1,tzinfo=timezone.utc)
        collector = TelegramCollector(collector_support.CONFIG,backend=backend,client_factory=lambda *_:client)
        collector.owner = str(self.owner)
        collector.client = client
        source = {'chat_id':'-1001','trusted_sender_ids':['7'],'allow_media':True}
        collector.sources = {'-1001':source}
        original = backend.acknowledge_observed
        calls = []
        async def lose_ack(*args):
            result = await original(*args)
            calls.append(1)
            if len(calls)==1:
                raise httpx.ReadTimeout('test-only lost ack response')
            return result
        backend.acknowledge_observed = lose_ack
        try:
            with self.assertRaises(httpx.ReadTimeout):
                await collector._reconcile_known('-1001',source)
            self.assertTrue(collector.reconcile_pending['-1001'][0]['_reconcile_applied'])
            async def forbidden(*args, **kwargs):
                self.fail('ack retry must not require another Telegram request')
            client.get_entity = forbidden
            client.get_messages = forbidden
            result = await collector._reconcile_known('-1001',source)
            self.assertEqual(1,result['checked'])
            self.assertEqual(2,len(calls))
            self.assertEqual(2,await self.pool.fetchval('SELECT count(*) FROM v2_expert_messages'))
            self.assertEqual(1,await self.pool.fetchval("SELECT count(*) FROM v2_analysis_jobs WHERE state='queued'"))
        finally:
            await collector.disconnect()

    def collector_for(self, backend, client):
        collector = TelegramCollector(collector_support.CONFIG,backend=backend,client_factory=lambda *_:client)
        collector.owner = str(self.owner)
        collector.client = client
        source = {'chat_id':'-1001','trusted_sender_ids':['7'],'allow_media':True}
        collector.sources = {'-1001':source}
        return collector,source

    async def test_lost_delete_event_response_replays_exact_payload_and_one_tombstone(self):
        await self.seed()
        backend = TelegramBackend(self.client,'http://testserver/internal/telegram')
        client = collector_support.Client({'-1001':[]})
        collector,source = self.collector_for(backend,client)
        original = backend.event
        calls = []
        async def lost_response(body):
            result = await original(body)
            calls.append(body)
            if len(calls)==1:
                raise httpx.ReadTimeout('test-only committed event response lost')
            return result
        backend.event = lost_response
        try:
            with self.assertRaises(httpx.ReadTimeout):
                await collector._reconcile_known('-1001',source)
            self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_telegram_tombstones'))
            async def forbidden(*args, **kwargs):
                self.fail('uncertain write recovery must replay identical payload before new RPC')
            client.get_entity = forbidden
            client.get_messages = forbidden
            result = await collector._reconcile_known('-1001',source)
            self.assertEqual(1,result['checked'])
            self.assertEqual(calls[0],calls[1])
            self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_telegram_tombstones'))
            self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_expert_messages'))
            self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_telegram_events WHERE reconcile_acknowledged_at IS NOT NULL'))
        finally:
            await collector.disconnect()

    async def test_cancelled_rpc_restart_recovers_remote_claim_after_expiry_without_delete(self):
        body = await self.seed()
        backend = TelegramBackend(self.client,'http://testserver/internal/telegram')
        message = collector_support.Message(11)
        message.message = body['raw_text']
        client = collector_support.Client({'-1001':[message]})
        collector,source = self.collector_for(backend,client)
        original = client.get_messages
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Future()
        client.get_messages = blocked
        task = asyncio.create_task(collector._reconcile_known('-1001',source))
        await asyncio.wait_for(entered.wait(),2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_telegram_tombstones'))
        self.assertIsNone(await self.pool.fetchval('SELECT reconcile_applied_at FROM v2_telegram_events'))
        claimed = collector.reconcile_pending['-1001'][0]
        await collector.disconnect()
        await self.expire(claimed)
        client.get_messages = original
        restarted,source = self.collector_for(backend,client)
        try:
            result = await restarted._reconcile_known('-1001',source)
            self.assertEqual(1,result['checked'])
            self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_telegram_tombstones'))
            self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_telegram_events WHERE reconcile_acknowledged_at IS NOT NULL'))
            self.assertIsNotNone(await self.pool.fetchval('SELECT next_check_at FROM v2_telegram_events WHERE is_current'))
        finally:
            await restarted.disconnect()

    async def test_takeover_owner_invalidates_old_collector_and_lost_token(self):
        body = await self.seed()
        item, = await self.observe()
        await self.pool.execute("UPDATE v2_source_checkpoints SET lease_until=now()-interval '1 second'")
        new_owner = uuid.uuid4()
        response = await self.client.post('/internal/telegram/checkpoints/claim',json={
            'chat_id':'-1001','owner':str(new_owner)})
        self.assertEqual(200,response.status_code)
        self.assertEqual(409,(await self.checked(body,item)).status_code)
        await self.expire(item)
        self.owner = new_owner
        reclaimed, = await self.observe()
        self.assertNotEqual(item['claim_id'],reclaimed['claim_id'])
        self.assertEqual(409,(await self.checked(body | {'owner':str(new_owner)},item)).status_code)
        self.assertEqual(200,(await self.checked(body | {'owner':str(new_owner)},reclaimed)).status_code)
        self.assertEqual(200,(await self.ack(reclaimed)).status_code)

    async def test_migration_preserves_existing_event_and_reapplication_is_noop(self):
        name = 'telegram_migrate_' + uuid.uuid4().hex
        await self.control.execute(f'CREATE DATABASE "{name}"')
        conn = await asyncpg.connect(support.TEST_URL.rsplit('/',1)[0]+'/'+name)
        try:
            with tempfile.TemporaryDirectory() as directory:
                for path in database.MIGRATIONS.glob('[0-9]*_*.sql'):
                    if path.name<'019_':
                        Path(directory,path.name).write_text(path.read_text())
                await database.apply_migrations(conn,Path(directory))
            source,event = uuid.uuid4(),uuid.uuid4()
            await conn.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'-1009','迁移样本')",source)
            await conn.execute("INSERT INTO v2_telegram_events(id,source_id,platform_message_id,version_key,source_version_at,event_kind,content,media_state,next_check_at) "
                               "VALUES($1,$2,'99',$3,now(),'message','{}','ready',now()-interval '1 hour')",event,source,hashlib.sha256(b'migration').hexdigest())
            before = dict(await conn.fetchrow('SELECT * FROM v2_telegram_events'))
            self.assertEqual(['019_telegram_reconciliation_claims.sql'],await database.apply_migrations(conn))
            after = dict(await conn.fetchrow('SELECT * FROM v2_telegram_events'))
            self.assertEqual(before,{key:after[key] for key in before})
            self.assertTrue(all(after[key] is None for key in set(after)-set(before)))
            self.assertEqual([],await database.apply_migrations(conn))
            await database.check_schema(conn)
        finally:
            await conn.close()
            await self.control.execute(f'DROP DATABASE "{name}"')
