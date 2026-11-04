"""Durable reconciliation with an explicitly temporary PostgreSQL only."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

import asyncpg
import httpx

from app import database
from app.telegram_reconciliation import _lease_events
from collector.telegram import TelegramCollector
from collector.telegram_backend import TelegramBackend
import test_telegram_api as support
import test_telegram_collector as collector_support


class ReconciliationBatchLeaseUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_lease_uses_one_database_roundtrip_and_preserves_selected_order(self):
        selected = [{'id': uuid.uuid4(), 'platform_message_id': str(i)} for i in range(3)]

        async def shuffled_database_return(query, event_ids, claim_ids, *_):
            return [
                {'id': event_id, 'reconcile_claim_id': claim_id,
                 'reconcile_lease_until': datetime.now(timezone.utc) + timedelta(seconds=30)}
                for event_id, claim_id in reversed(list(zip(event_ids, claim_ids)))
            ]

        conn = SimpleNamespace(fetch=AsyncMock(side_effect=shuffled_database_return))

        request_id = uuid.uuid4()
        result = await _lease_events(conn, selected, 'collector-owner', request_id)

        conn.fetch.assert_awaited_once()
        query = conn.fetch.await_args.args[0]
        self.assertIn('UPDATE v2_telegram_events', query)
        self.assertIn('reconcile_request_id=$4', query)
        self.assertIn('unnest', query)
        self.assertEqual([row['id'] for row in selected], [row['id'] for row in result])
        self.assertEqual(3, len({row['claim_id'] for row in result}))

    async def test_empty_batch_does_not_open_a_database_roundtrip(self):
        conn = SimpleNamespace(fetch=AsyncMock())
        self.assertEqual([], await _lease_events(conn, [], 'collector-owner', uuid.uuid4()))
        conn.fetch.assert_not_awaited()


class ReconciliationTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_503_confirms_previous_then_retries_same_cas_once(self):
        requests = []
        delays = []
        checkpoint = 12

        def handler(request):
            nonlocal checkpoint
            requests.append(request)
            if request.url.path.endswith('/checkpoints/claim'):
                return httpx.Response(200, json={'last_message_id': checkpoint, 'lease_until': 'future'})
            if len([item for item in requests if item.url.path.endswith('/checkpoints')]) == 1:
                return httpx.Response(503, json={}, headers={'Retry-After': '1'})
            checkpoint = 13
            return httpx.Response(200, json={'last_message_id': checkpoint})

        async def record_sleep(delay):
            delays.append(delay)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            with patch('collector.telegram_backend.asyncio.sleep', new=record_sleep):
                with self.assertLogs('platform.telegram', level='WARNING') as logs:
                    result = await backend.advance('-1001', 'private-owner-only', 13, 12)

        puts = [request for request in requests if request.method == 'PUT']
        self.assertEqual({'last_message_id': 13}, result)
        self.assertEqual(2, len(puts))
        self.assertEqual(puts[0].content, puts[1].content)
        self.assertEqual([1.0], delays)
        self.assertNotIn('private-owner-only', '\n'.join(logs.output))

    async def test_checkpoint_503_after_commit_is_confirmed_without_retry(self):
        requests = []
        checkpoint = 12

        def handler(request):
            nonlocal checkpoint
            requests.append(request)
            if request.method == 'PUT':
                checkpoint = 13
                return httpx.Response(503, json={})
            return httpx.Response(200, json={'last_message_id': checkpoint, 'lease_until': 'future'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            with self.assertLogs('platform.telegram', level='WARNING'):
                result = await backend.advance('-1001', 'same-owner', 13, 12)

        self.assertEqual({'last_message_id': 13}, result)
        self.assertEqual(['PUT', 'POST'], [request.method for request in requests])

    async def test_checkpoint_timeout_after_commit_is_confirmed_without_retry(self):
        requests = []
        checkpoint = 12

        def handler(request):
            nonlocal checkpoint
            requests.append(request)
            if request.method == 'PUT':
                checkpoint = 13
                raise httpx.ReadTimeout('private transport detail', request=request)
            return httpx.Response(200, json={'last_message_id': checkpoint, 'lease_until': 'future'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            with patch('collector.telegram_backend.asyncio.sleep', new=AsyncMock()) as pause:
                result = await backend.advance('-1001', 'same-owner', 13, 12)

        self.assertEqual({'last_message_id': 13}, result)
        self.assertEqual(['PUT', 'POST'], [request.method for request in requests])
        pause.assert_not_awaited()

    async def test_second_ambiguous_cas_is_confirmed_after_commit(self):
        requests = []
        delays = []
        checkpoint = 12
        put_count = 0

        def handler(request):
            nonlocal checkpoint, put_count
            requests.append(request)
            if request.method == 'PUT':
                put_count += 1
                if put_count == 2:
                    checkpoint = 13
                return httpx.Response(503, json={})
            return httpx.Response(200, json={'last_message_id': checkpoint, 'lease_until': 'future'})

        async def record_sleep(delay):
            delays.append(delay)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            with patch('collector.telegram_backend.asyncio.sleep', new=record_sleep):
                with self.assertLogs('platform.telegram', level='WARNING'):
                    result = await backend.advance('-1001', 'same-owner', 13, 12)

        self.assertEqual({'last_message_id': 13}, result)
        self.assertEqual(2, put_count)
        self.assertEqual(2, sum(request.url.path.endswith('/checkpoints/claim') for request in requests))
        self.assertEqual([0.5], delays)

    async def test_second_ambiguous_cas_without_commit_stops_after_one_retry(self):
        requests = []
        delays = []

        def handler(request):
            requests.append(request)
            if request.method == 'PUT':
                return httpx.Response(503, json={})
            return httpx.Response(200, json={'last_message_id': 12, 'lease_until': 'future'})

        async def record_sleep(delay):
            delays.append(delay)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            with patch('collector.telegram_backend.asyncio.sleep', new=record_sleep):
                with self.assertLogs('platform.telegram', level='WARNING'):
                    with self.assertRaises(httpx.HTTPStatusError) as raised:
                        await backend.advance('-1001', 'same-owner', 13, 12)

        self.assertEqual(503, raised.exception.response.status_code)
        self.assertEqual(2, sum(request.method == 'PUT' for request in requests))
        self.assertEqual(2, sum(request.url.path.endswith('/checkpoints/claim') for request in requests))
        self.assertEqual([0.5], delays)

    async def test_checkpoint_owner_confirmation_failure_stops_without_retry(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.method == 'PUT':
                return httpx.Response(503, json={})
            return httpx.Response(409, json={'detail': 'Telegram source is owned by another collector'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            with self.assertLogs('platform.telegram', level='WARNING'):
                with self.assertRaises(httpx.HTTPStatusError) as raised:
                    await backend.advance('-1001', 'old-owner', 13, 12)

        self.assertEqual(409, raised.exception.response.status_code)
        self.assertEqual(['PUT', 'POST'], [request.method for request in requests])

    async def test_checkpoint_unexpected_high_water_stops_without_retry(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.method == 'PUT':
                return httpx.Response(503, json={})
            return httpx.Response(200, json={'last_message_id': 11, 'lease_until': 'future'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            with self.assertLogs('platform.telegram', level='WARNING'):
                with self.assertRaisesRegex(RuntimeError, 'checkpoint changed'):
                    await backend.advance('-1001', 'same-owner', 13, 10)

        self.assertEqual(['PUT', 'POST'], [request.method for request in requests])

    async def test_checkpoint_conflict_is_not_claimed_or_retried(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(409, json={'detail': 'checkpoint has changed'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            with self.assertLogs('platform.telegram', level='WARNING'):
                with self.assertRaises(httpx.HTTPStatusError):
                    await backend.advance('-1001', 'same-owner', 13, 12)

        self.assertEqual(['PUT'], [request.method for request in requests])

    async def test_rejection_logs_only_stable_reason_and_preserves_original_response(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(409, json={'detail': 'same message version has conflicting content'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            with self.assertLogs('platform.telegram', level='WARNING') as logs:
                with self.assertRaises(httpx.HTTPStatusError) as raised:
                    await backend.event({'raw_text': 'private-message-only', 'owner': 'private-owner-only'})
        self.assertEqual(409, raised.exception.response.status_code)
        self.assertEqual(1, len(requests))
        self.assertIn('operation=/events status=409 reason=version_content_conflict', logs.output[0])
        self.assertNotIn('private-', logs.output[0])
        self.assertNotIn('conflicting content', logs.output[0])

    async def test_unrecognized_validation_payload_is_not_logged_or_retried(self):
        for payload in ({'detail': 'private-response-only'}, {'detail': [{'input': 'private-input-only'}]}, ['private-list-only']):
            requests = []
            def handler(request):
                requests.append(request)
                return httpx.Response(422, json=payload)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
                with self.assertLogs('platform.telegram', level='WARNING') as logs:
                    with self.assertRaises(httpx.HTTPStatusError):
                        await backend.observed('-1001', 'private-owner-only')
            self.assertEqual(1, len(requests))
            self.assertIn('reason=unclassified', logs.output[0])
            self.assertNotIn('private-', logs.output[0])

    async def test_empty_bridge_unavailable_response_has_bounded_reason(self):
        def handler(request):
            return httpx.Response(503, json={}, headers={'Retry-After': '0'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            with self.assertLogs('platform.telegram', level='WARNING') as logs:
                with self.assertRaises(httpx.HTTPStatusError):
                    await backend.checkpoint('-1001', 'private-owner-only')
        self.assertTrue(any('operation=/checkpoints/claim status=503 reason=service_unavailable' in line for line in logs.output))
        self.assertNotIn('private-', logs.output[0])

    async def test_idempotent_event_retries_database_503_once_with_identical_body(self):
        requests=[]
        delays=[]
        def handler(request):
            requests.append((request.url.path,request.content))
            if len(requests)==1:
                return httpx.Response(503,json={'code':'database_temporarily_unavailable'},headers={'Retry-After':'2'})
            return httpx.Response(200,json={'persisted':True,'state':'ready','message_id':'durable-id'})
        async def record_sleep(delay):
            delays.append(delay)
        body={'chat_id':'-1001','owner':'private-owner-only','platform_message_id':'991',
              'version_key':'a'*64,'raw_text':'private-message-only'}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend=TelegramBackend(client,'http://isolated.invalid/internal/telegram')
            with patch('collector.telegram_backend.asyncio.sleep',new=record_sleep):
                with self.assertLogs('platform.telegram',level='WARNING') as logs:
                    result=await backend.event(body)
        self.assertEqual({'persisted':True,'state':'ready','message_id':'durable-id'},result)
        self.assertEqual(2,len(requests))
        self.assertEqual(requests[0],requests[1])
        self.assertEqual([2.0],delays)
        self.assertIn('operation=/events status=503 attempt=1 retry_after_s=2.000',logs.output[0])
        self.assertNotIn('private-', '\n'.join(logs.output))

    async def test_idempotent_retry_after_is_bounded_and_invalid_values_fall_back(self):
        for header, expected in ((None,0.5),('invalid',0.5),('-1',0.5),('nan',0.5),('999',2.0)):
            with self.subTest(retry_after=header):
                requests=[];delays=[]
                def handler(request):
                    requests.append(request.content)
                    if len(requests)==1:
                        headers={} if header is None else {'Retry-After':header}
                        return httpx.Response(503,json={'code':'database_temporarily_unavailable'},headers=headers)
                    return httpx.Response(200,json={'persisted':True,'state':'ready','message_id':'durable-id'})
                async def record_sleep(delay):delays.append(delay)
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    backend=TelegramBackend(client,'http://isolated.invalid/internal/telegram')
                    with patch('collector.telegram_backend.asyncio.sleep',new=record_sleep):
                        await backend.event({'chat_id':'-1001','platform_message_id':'991','version_key':'a'*64})
                self.assertEqual([expected],delays)
                self.assertEqual(2,len(requests))
                self.assertEqual(requests[0],requests[1])

    async def test_idempotent_retry_stops_after_second_database_503(self):
        requests=[];delays=[]
        def handler(request):
            requests.append(request.content)
            return httpx.Response(503,json={'code':'database_temporarily_unavailable'},headers={'Retry-After':'1'})
        async def record_sleep(delay):delays.append(delay)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend=TelegramBackend(client,'http://isolated.invalid/internal/telegram')
            with patch('collector.telegram_backend.asyncio.sleep',new=record_sleep):
                with self.assertRaises(httpx.HTTPStatusError) as raised:
                    await backend.event({'chat_id':'-1001','platform_message_id':'991','version_key':'a'*64})
        self.assertEqual(503,raised.exception.response.status_code)
        self.assertEqual(2,len(requests))
        self.assertEqual([1.0],delays)
        self.assertEqual(requests[0],requests[1])

    async def test_idempotent_claim_retries_once_with_same_request_body(self):
        for operation,call,delay in (
            ('/checkpoints/claim',lambda backend:backend.checkpoint('-1001','same-owner'),2.0),
            ('/observed',lambda backend:backend.observed('-1001','same-owner'),0.25),
        ):
            with self.subTest(operation=operation):
                requests=[]
                delays=[]
                def handler(request):
                    requests.append(request)
                    if len(requests)==1:
                        return httpx.Response(503,json={'code':'database_temporarily_unavailable'},
                                              headers={'Retry-After':str(delay)})
                    if operation=='/checkpoints/claim':
                        return httpx.Response(200,json={'last_message_id':12,'lease_until':'future'})
                    return httpx.Response(200,json={'items':[],'claim_lease_seconds':30})
                async def record_sleep(seconds):delays.append(seconds)
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    backend=TelegramBackend(client,'http://isolated.invalid/internal/telegram')
                    with patch('collector.telegram_backend.asyncio.sleep',new=record_sleep):
                        with self.assertLogs('platform.telegram',level='WARNING') as logs:
                            result=await call(backend)
                self.assertEqual(2,len(requests))
                self.assertEqual(requests[0].content,requests[1].content)
                self.assertEqual([delay],delays)
                self.assertIn('attempt=1 retry_after_s=',logs.output[0])
                if operation=='/checkpoints/claim':
                    self.assertEqual(12,result['last_message_id'])
                else:
                    payload=json.loads(requests[0].content)
                    self.assertEqual([],result)
                    self.assertEqual(str(uuid.UUID(payload['claim_request_id'])),payload['claim_request_id'])

    async def test_observed_timeout_retries_same_request_once(self):
        requests=[]
        delays=[]
        def handler(request):
            requests.append(request)
            if len(requests)==1:
                raise httpx.ReadTimeout('transport detail',request=request)
            return httpx.Response(200,json={'items':[],'claim_lease_seconds':30})
        async def record_sleep(seconds):delays.append(seconds)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend=TelegramBackend(client,'http://isolated.invalid/internal/telegram')
            with patch('collector.telegram_backend.asyncio.sleep',new=record_sleep):
                with self.assertLogs('platform.telegram',level='WARNING') as logs:
                    self.assertEqual([],await backend.observed('-1001','same-owner'))
        self.assertEqual(2,len(requests))
        self.assertEqual(requests[0].content,requests[1].content)
        self.assertEqual([0.5],delays)
        self.assertIn('category=timeout attempt=1',logs.output[0])

    async def test_database_rejection_code_has_bounded_reason(self):
        def handler(request):
            return httpx.Response(503, json={
                'detail': '内部数据库错误不得写入日志',
                'code': 'database_temporarily_unavailable',
            })
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            with self.assertLogs('platform.telegram', level='WARNING') as logs:
                with self.assertRaises(httpx.HTTPStatusError):
                    await backend.checkpoint('-1001', 'private-owner-only')
        self.assertTrue(any('reason=database_temporarily_unavailable' in line for line in logs.output))
        self.assertNotIn('内部数据库错误', logs.output[0])

    async def test_event_and_observed_overlap_checkpoint_while_checkpoint_writes_stay_ordered(self):
        checkpoint_started = asyncio.Event()
        event_and_observed_started = asyncio.Event()
        release_checkpoint = asyncio.Event()
        requests = []

        async def handler(request):
            path = request.url.path
            requests.append(path)
            if path == '/internal/telegram/checkpoints/claim':
                checkpoint_started.set()
                await release_checkpoint.wait()
            elif path in {'/internal/telegram/events', '/internal/telegram/observed'}:
                if {'/internal/telegram/events', '/internal/telegram/observed'} <= set(requests):
                    event_and_observed_started.set()
            if path == '/internal/telegram/observed':
                return httpx.Response(200, json={'items': []})
            if path == '/internal/telegram/checkpoints':
                return httpx.Response(200, json={'last_message_id': 1})
            return httpx.Response(200, json={'persisted': True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            checkpoint = asyncio.create_task(backend.checkpoint('-1001', 'test-owner'))
            await asyncio.wait_for(checkpoint_started.wait(), timeout=1)
            event = asyncio.create_task(backend.event({'chat_id': '-1001', 'owner': 'test-owner'}))
            observed = asyncio.create_task(backend.observed('-1001', 'test-owner'))
            advance = asyncio.create_task(backend.advance('-1001', 'test-owner', 1, 0))
            await asyncio.wait_for(event_and_observed_started.wait(), timeout=1)
            self.assertNotIn('/internal/telegram/checkpoints', requests)
            release_checkpoint.set()
            await asyncio.gather(checkpoint, event, observed, advance)
        self.assertEqual(1, requests.count('/internal/telegram/checkpoints/claim'))
        self.assertEqual(1, requests.count('/internal/telegram/checkpoints'))

    async def test_same_message_event_writes_serialize_without_blocking_other_messages(self):
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        other_started = asyncio.Event()
        release_first = asyncio.Event()
        requests = []

        async def handler(request):
            body = json.loads(request.content)
            message_id = body['platform_message_id']
            requests.append(message_id)
            if message_id == 'same' and requests.count('same') == 1:
                first_started.set()
                await release_first.wait()
            elif message_id == 'same':
                second_started.set()
            else:
                other_started.set()
            return httpx.Response(200, json={'persisted': True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            first = asyncio.create_task(backend.event({'chat_id': '-1001', 'platform_message_id': 'same'}))
            await asyncio.wait_for(first_started.wait(), timeout=1)
            duplicate = asyncio.create_task(backend.event({'chat_id': '-1001', 'platform_message_id': 'same'}))
            other = asyncio.create_task(backend.event({'chat_id': '-1001', 'platform_message_id': 'other'}))
            await asyncio.wait_for(other_started.wait(), timeout=1)
            await asyncio.sleep(0)
            self.assertFalse(second_started.is_set())
            release_first.set()
            await asyncio.gather(first, duplicate, other)

        self.assertTrue(second_started.is_set())
        self.assertEqual(['same', 'other', 'same'], requests)
        self.assertEqual({}, backend._event_locks)

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
        payload=json.loads(requests[0].content)
        self.assertEqual({'chat_id': '-1001', 'owner': 'test-owner-only', 'limit': 10,
                          'reconciliation_protocol': 'claim-ack-v1',
                          'claim_request_id': payload['claim_request_id']}, payload)
        self.assertEqual(str(uuid.UUID(payload['claim_request_id'])),payload['claim_request_id'])

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

    async def observe(self, *, limit=10, **kwargs):
        result = await self.client.post('/internal/telegram/observed', json={
            'chat_id': '-1001', 'owner': str(self.owner), 'limit': limit,
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

    async def test_event_request_commits_and_acknowledges_claim_atomically(self):
        body = await self.seed()
        due = await self.pool.fetchval('SELECT next_check_at FROM v2_telegram_events')
        item, = await self.observe()
        self.assertEqual(due, await self.pool.fetchval('SELECT next_check_at FROM v2_telegram_events'))
        self.assertEqual([], await self.observe())
        self.assertEqual(409, (await self.ack(item)).status_code)
        applied = await self.checked(body, item)
        self.assertEqual(200, applied.status_code, applied.text)
        self.assertTrue(applied.json()['reconciliation_acknowledged'])
        self.assertFalse(applied.json()['reconciliation_ack_duplicate'])
        after = await self.pool.fetchrow('SELECT next_check_at,reconcile_acknowledged_at FROM v2_telegram_events')
        self.assertGreater(after['next_check_at'], datetime.now(timezone.utc))
        self.assertIsNotNone(await self.pool.fetchval('SELECT reconcile_applied_at FROM v2_telegram_events'))
        self.assertEqual({'acknowledged': True, 'duplicate': True}, (await self.ack(item)).json())
        self.assertEqual({'acknowledged': True, 'duplicate': True}, (await self.ack(item)).json())
        self.assertEqual(after, await self.pool.fetchrow('SELECT next_check_at,reconcile_acknowledged_at FROM v2_telegram_events'))

    async def test_same_observation_request_replays_active_claim_without_extending_lease(self):
        await self.seed()
        request_id=uuid.uuid4()
        first,=await self.observe(claim_request_id=str(request_id))
        before=await self.pool.fetchrow(
            'SELECT reconcile_claim_id,reconcile_request_id,reconcile_lease_until FROM v2_telegram_events')
        replay,=await self.observe(claim_request_id=str(request_id))
        after=await self.pool.fetchrow(
            'SELECT reconcile_claim_id,reconcile_request_id,reconcile_lease_until FROM v2_telegram_events')
        self.assertEqual(first['claim_id'],replay['claim_id'])
        self.assertEqual(first['claim_lease_until'],replay['claim_lease_until'])
        self.assertEqual(before,after)
        self.assertEqual(request_id,before['reconcile_request_id'])
        self.assertEqual([],await self.observe(claim_request_id=str(uuid.uuid4())))

    async def test_concurrent_duplicate_observation_request_returns_one_claim(self):
        await self.seed()
        request_id=str(uuid.uuid4())
        first,second=await asyncio.gather(
            self.observe(claim_request_id=request_id),
            self.observe(claim_request_id=request_id),
        )
        self.assertEqual(1,len(first))
        self.assertEqual(first,second)
        persisted=await self.pool.fetchrow(
            'SELECT reconcile_claim_id,reconcile_request_id FROM v2_telegram_events')
        self.assertEqual(uuid.UUID(first[0]['claim_id']),persisted['reconcile_claim_id'])
        self.assertEqual(uuid.UUID(request_id),persisted['reconcile_request_id'])

    async def test_acknowledged_claim_is_not_replayed_by_old_request(self):
        body=await self.seed()
        request_id=uuid.uuid4()
        item,=await self.observe(claim_request_id=str(request_id))
        self.assertEqual(200,(await self.checked(body,item)).status_code)
        self.assertEqual(200,(await self.ack(item)).status_code)
        self.assertIsNone(await self.pool.fetchval('SELECT reconcile_request_id FROM v2_telegram_events'))
        self.assertEqual([],await self.observe(claim_request_id=str(request_id)))

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
        self.assertTrue(result.json()['reconciliation_acknowledged'])
        next_due = await self.pool.fetchval('SELECT next_check_at FROM v2_telegram_events WHERE is_current')
        self.assertEqual(200, (await self.ack(item)).status_code)
        self.assertEqual(next_due, await self.pool.fetchval('SELECT next_check_at FROM v2_telegram_events WHERE is_current'))
        self.assertEqual('赛前阿森纳大2.5', await self.pool.fetchval(
            'SELECT m.raw_text FROM v2_telegram_events e JOIN v2_expert_messages m ON m.id=e.message_id WHERE e.id=$1', uuid.UUID(item['id'])))
        self.assertEqual('reconciled', (await self.checked(edit, item)).json()['state'])
        self.assertEqual(409, (await self.checked(edit | {'raw_text': '冲突回包不可覆盖'}, item)).status_code)
        self.assertEqual(2, await self.pool.fetchval('SELECT count(*) FROM v2_expert_messages'))

    async def test_ack_failure_rolls_back_reconciliation_event_and_review_job(self):
        await self.seed()
        item, = await self.observe()
        await self.pool.execute("""
            CREATE FUNCTION reject_reconciliation_ack() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.reconcile_acknowledged_at IS NOT NULL AND OLD.reconcile_acknowledged_at IS NULL THEN
                RAISE EXCEPTION 'forced acknowledgement failure';
              END IF;
              RETURN NEW;
            END $$;
        """)
        await self.pool.execute("""
            CREATE TRIGGER reject_reconciliation_ack BEFORE UPDATE ON v2_telegram_events
            FOR EACH ROW EXECUTE FUNCTION reject_reconciliation_ack()
        """)
        edit = self.event(version='v2', text='修改应回滚', kind='edit', minutes=1)
        with self.assertRaises(Exception):
            await self.checked(edit, item)
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_messages'))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_telegram_events'))
        current = await self.pool.fetchrow('SELECT is_current,reconcile_applied_at,reconcile_acknowledged_at FROM v2_telegram_events')
        self.assertTrue(current['is_current'])
        self.assertIsNone(current['reconcile_applied_at'])
        self.assertIsNone(current['reconcile_acknowledged_at'])
        self.assertEqual('赛前阿森纳大2.5', await self.pool.fetchval('SELECT raw_text FROM v2_expert_messages'))

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

    async def test_recent_trusted_events_precede_fifo_history_and_untrusted_can_become_eligible(self):
        now = datetime.now(timezone.utc)
        fixtures = (
            (11, '7', timedelta(days=3), timedelta(minutes=2)),
            (12, '7', timedelta(days=4), timedelta(minutes=1)),
            (13, '7', timedelta(minutes=10), timedelta(minutes=2)),
            (14, '7', timedelta(minutes=1), timedelta(minutes=1)),
            (15, '7', timedelta(minutes=2), timedelta(minutes=2)),
        )
        for number, sender_id, age, due_age in fixtures:
            body = self.event(version=f'priority-{number}')
            body.update(platform_message_id=str(number), member_message_ids=[number])
            self.assertEqual(200, (await self.post(body)).status_code)
            await self.pool.execute(
                'UPDATE v2_telegram_events SET source_version_at=$2,next_check_at=$3 WHERE platform_message_id=$1',
                str(number), now - age, now - due_age,
            )
            if number == 15:
                await self.pool.execute(
                    "UPDATE v2_telegram_events SET content=jsonb_set(content,'{sender_id}','\"99\"'::jsonb) "
                    "WHERE platform_message_id='15'"
                )

        first = await self.observe(limit=3)
        self.assertEqual(['14', '13', '11'], [item['platform_message_id'] for item in first])
        second = await self.observe()
        self.assertEqual(['12'], [item['platform_message_id'] for item in second])
        self.assertIsNone(await self.pool.fetchval(
            "SELECT reconcile_claim_id FROM v2_telegram_events WHERE platform_message_id='15'"))

        late_expert = uuid.uuid4()
        await self.pool.execute(
            "INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'99','后续授权专家')",
            late_expert,
        )
        await self.pool.execute('INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)',
                                self.source_id, late_expert)
        third = await self.observe()
        self.assertEqual(['15'], [item['platform_message_id'] for item in third])

    async def test_recent_only_batch_fills_unused_history_capacity_without_duplicates(self):
        now = datetime.now(timezone.utc)
        for number in range(11, 15):
            await self.seed(number)
            await self.pool.execute(
                'UPDATE v2_telegram_events SET source_version_at=$2 WHERE platform_message_id=$1',
                str(number), now - timedelta(minutes=15-number),
            )
        first = await self.observe(limit=3)
        self.assertEqual(['14', '13', '12'], [item['platform_message_id'] for item in first])
        self.assertEqual(3, len({item['claim_id'] for item in first}))
        second = await self.observe(limit=3)
        self.assertEqual(['11'], [item['platform_message_id'] for item in second])
        self.assertEqual([], await self.observe(limit=3))

    async def test_history_only_batch_fills_unused_recent_capacity_in_fifo_order(self):
        now = datetime.now(timezone.utc)
        for number in range(11, 15):
            await self.seed(number)
            await self.pool.execute(
                'UPDATE v2_telegram_events SET source_version_at=$2,next_check_at=$3 '
                'WHERE platform_message_id=$1', str(number), now-timedelta(days=3),
                now-timedelta(minutes=number),
            )
        first = await self.observe(limit=3)
        self.assertEqual(['14', '13', '12'], [item['platform_message_id'] for item in first])
        second = await self.observe(limit=3)
        self.assertEqual(['11'], [item['platform_message_id'] for item in second])

    async def test_locked_newest_recent_event_does_not_block_mixed_batch_or_disappear(self):
        now = datetime.now(timezone.utc)
        for number, age in ((11, 1), (12, 2), (13, 3), (14, 180)):
            await self.seed(number)
            await self.pool.execute(
                'UPDATE v2_telegram_events SET source_version_at=$2 WHERE platform_message_id=$1',
                str(number), now-timedelta(minutes=age),
            )
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetchval(
                    "SELECT id FROM v2_telegram_events WHERE platform_message_id='11' FOR UPDATE")
                first = await self.observe(limit=3)
                self.assertEqual(['12', '13', '14'], [item['platform_message_id'] for item in first])
        second = await self.observe(limit=3)
        self.assertEqual(['11'], [item['platform_message_id'] for item in second])

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

    async def test_batch_lease_rows_keep_selected_order_and_distinct_persisted_claims(self):
        for number in (11, 12, 13):
            await self.seed(number, version=f'batch-{number}')

        items = await self.observe(limit=3)
        selected = await self.pool.fetch(
            "SELECT id,platform_message_id FROM v2_telegram_events "
            "WHERE reconcile_claim_id IS NOT NULL ORDER BY source_version_at DESC,next_check_at,id"
        )
        leases = await self.pool.fetch(
            'SELECT id,reconcile_claim_id,reconcile_owner,reconcile_lease_until FROM v2_telegram_events '
            'WHERE reconcile_claim_id IS NOT NULL'
        )
        by_id = {row['id']: row for row in leases}
        self.assertEqual([row['id'] for row in selected], [uuid.UUID(item['id']) for item in items])
        self.assertEqual(3, len({item['claim_id'] for item in items}))
        for item in items:
            lease = by_id[uuid.UUID(item['id'])]
            self.assertEqual(uuid.UUID(item['claim_id']), lease['reconcile_claim_id'])
            self.assertEqual(self.owner, lease['reconcile_owner'])
            self.assertEqual(datetime.fromisoformat(item['claim_lease_until']), lease['reconcile_lease_until'])

    async def test_batch_lease_failure_rolls_back_every_claim(self):
        for number in (11, 12, 13):
            await self.seed(number, version=f'rollback-{number}')
        await self.pool.execute("""
            CREATE FUNCTION reject_reconciliation_lease() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.reconcile_claim_id IS DISTINCT FROM OLD.reconcile_claim_id THEN
                RAISE EXCEPTION 'forced batch lease failure';
              END IF;
              RETURN NEW;
            END $$;
        """)
        await self.pool.execute("""
            CREATE TRIGGER reject_reconciliation_lease BEFORE UPDATE ON v2_telegram_events
            FOR EACH ROW EXECUTE FUNCTION reject_reconciliation_lease()
        """)

        with self.assertRaises(Exception):
            await self.observe(limit=3)

        rows = await self.pool.fetch(
            'SELECT reconcile_claim_id,reconcile_lease_until,next_check_at FROM v2_telegram_events'
        )
        self.assertEqual(3, len(rows))
        self.assertTrue(all(row['reconcile_claim_id'] is None and row['reconcile_lease_until'] is None
                            and row['next_check_at'] < datetime.now(timezone.utc) for row in rows))

    async def test_collector_lost_atomic_event_response_replays_then_skips_separate_ack(self):
        await self.seed()
        backend = TelegramBackend(self.client,'http://testserver/internal/telegram')
        client = collector_support.Client({'-1001':[collector_support.Message(11)]})
        client.messages['-1001'][0].edit_date = (
            await self.pool.fetchval('SELECT source_version_at FROM v2_telegram_events')
        ) + timedelta(minutes=1)
        collector = TelegramCollector(collector_support.CONFIG,backend=backend,client_factory=lambda *_:client)
        collector.owner = str(self.owner)
        collector.client = client
        source = {'chat_id':'-1001','trusted_sender_ids':['7'],'allow_media':True}
        collector.sources = {'-1001':source}
        original = backend.event
        calls = []
        async def lose_event_response(body):
            result = await original(body)
            calls.append(body)
            if len(calls)==1:
                raise httpx.ReadTimeout('test-only lost event response')
            return result
        backend.event = lose_event_response
        async def forbidden_ack(*args, **kwargs):
            self.fail('atomic event response already confirms the ACK')
        backend.acknowledge_observed = forbidden_ack
        try:
            with self.assertRaises(httpx.ReadTimeout):
                await collector._reconcile_known('-1001',source)
            claimed = collector.reconcile_pending['-1001'][0]
            self.assertNotIn('_reconcile_applied', claimed)
            self.assertTrue(claimed['_reconcile_submission']['payload'])
            self.assertIsNotNone(await self.pool.fetchval(
                'SELECT reconcile_acknowledged_at FROM v2_telegram_events WHERE id=$1',
                uuid.UUID(claimed['id'])))
            async def forbidden(*args, **kwargs):
                self.fail('ack retry must not require another Telegram request')
            client.get_entity = forbidden
            client.get_messages = forbidden
            result = await collector._reconcile_known('-1001',source)
            self.assertEqual(1,result['checked'])
            self.assertEqual(calls[0],calls[1])
            self.assertEqual(2,await self.pool.fetchval('SELECT count(*) FROM v2_expert_messages'))
            self.assertEqual(1,await self.pool.fetchval(
                "SELECT count(*) FROM v2_analysis_jobs WHERE state='unresolved' AND review_mode='historical_replay'"))
            self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_telegram_events WHERE reconcile_acknowledged_at IS NOT NULL'))
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
            self.assertEqual(['019_telegram_reconciliation_claims.sql','020_review_job_notifications.sql',
                              '021_review_search_all_phases.sql','022_result_observations.sql',
                              '023_telegram_author_backfill.sql','024_telegram_observation_request_id.sql',
                              '025_telegram_event_members_lookup.sql'],
                             await database.apply_migrations(conn))
            after = dict(await conn.fetchrow('SELECT * FROM v2_telegram_events'))
            self.assertEqual(before,{key:after[key] for key in before})
            self.assertTrue(all(after[key] is None for key in set(after)-set(before)))
            self.assertEqual([],await database.apply_migrations(conn))
            await database.check_schema(conn)
        finally:
            await conn.close()
            await self.control.execute(f'DROP DATABASE "{name}"')
