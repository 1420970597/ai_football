"""Owner-scoped handoff after quiescence, using isolated data only."""
import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch
import uuid

import httpx

from collector.telegram import TelegramCollector
from collector.telegram_backend import TelegramBackend
import test_telegram_api as fixtures


class LeaseReleaseTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_owner_request_has_its_own_bounded_transport_timeout(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={'released_sources': 2})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await TelegramBackend(client, 'http://isolated.invalid/internal/telegram').release_sources('owner')
        self.assertEqual({'released_sources': 2}, result)
        self.assertEqual(1, len(seen))
        self.assertEqual('/internal/telegram/checkpoints/release', seen[0].url.path)
        self.assertEqual('POST', seen[0].method)
        self.assertEqual({'owner': 'owner'}, json.loads(seen[0].content))
        self.assertEqual({'connect': 1, 'read': 3, 'write': 3, 'pool': 3}, seen[0].extensions['timeout'])

    async def test_ambiguous_or_failed_release_does_not_retry(self):
        for error in (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError):
            with self.subTest(error=error.__name__):
                seen = []

                def handler(request):
                    seen.append(request)
                    raise error('isolated failure', request=request)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(error):
                        await TelegramBackend(client, 'http://isolated.invalid').release_sources('owner')
                self.assertEqual(1, len(seen))


class LeaseReleaseLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self):
        self.backend = AsyncMock()
        self.backend.checkpoint.return_value = {'last_message_id': 35}
        self.backend.release_sources.return_value = {'released_sources': 1}
        return TelegramCollector({}, backend=self.backend)

    async def test_no_claim_means_no_release(self):
        collector = self.runtime()
        await collector.disconnect()
        self.backend.release_sources.assert_not_awaited()

    async def test_release_follows_drained_tasks_client_and_control(self):
        collector = self.runtime()
        await collector._checkpoint('-1001')
        order = []
        started = asyncio.Event()

        async def media():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                order.append('media_drained')

        task = asyncio.create_task(media())
        collector.live_media_tasks[('-1001', 'isolated')] = task
        await started.wait()
        collector.client = AsyncMock()
        collector.client.disconnect.side_effect = lambda: order.append('client_closed')
        collector.control_server = AsyncMock()
        collector.control_server.close.side_effect = lambda: order.append('control_closed')

        async def release(owner):
            self.assertEqual(collector.owner, owner)
            self.assertTrue(task.done())
            self.assertIsNone(collector.client)
            self.assertIsNone(collector.control_server)
            order.append('release')
            return {'released_sources': 1}

        self.backend.release_sources.side_effect = release
        await collector.disconnect()
        self.assertEqual(['media_drained', 'client_closed', 'control_closed', 'release'], order)
        await collector.disconnect()
        self.backend.release_sources.assert_awaited_once_with(collector.owner)

    async def test_ambiguous_claim_is_released_and_high_water_is_not_locally_changed(self):
        collector = self.runtime()
        self.backend.checkpoint.side_effect = httpx.ReadTimeout('isolated ambiguous response')
        with self.assertRaises(httpx.ReadTimeout):
            await collector._checkpoint('-1001')
        await collector.disconnect()
        self.backend.release_sources.assert_awaited_once_with(collector.owner)

    async def test_client_close_failure_does_not_release_before_quiescence(self):
        collector = self.runtime()
        await collector._checkpoint('-1001')
        collector.client = AsyncMock()
        collector.client.disconnect.side_effect = RuntimeError('isolated cleanup failure')
        with self.assertRaises(RuntimeError):
            await collector.disconnect()
        self.backend.release_sources.assert_not_awaited()

    async def test_failed_or_invalid_release_preserves_retryable_owner_state(self):
        for outcome in (RuntimeError('isolated failure'), {}, {'released_sources': True}, {'released_sources': -1}):
            with self.subTest(outcome=type(outcome).__name__):
                collector = self.runtime()
                await collector._checkpoint('-1001')
                self.backend.release_sources.side_effect = [outcome, {'released_sources': 0}]
                with self.assertRaises((RuntimeError, ValueError)):
                    await collector.disconnect()
                await collector.disconnect()
                self.assertEqual(2, self.backend.release_sources.await_count)

    async def test_cancelled_release_is_drained_and_not_marked_successful(self):
        collector = self.runtime()
        await collector._checkpoint('-1001')
        started, drained = asyncio.Event(), asyncio.Event()

        async def release(owner):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                drained.set()

        self.backend.release_sources.side_effect = release
        task = asyncio.create_task(collector.disconnect())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(drained.is_set())
        self.backend.release_sources.side_effect = None
        await collector.disconnect()
        self.assertEqual(2, self.backend.release_sources.await_count)

    async def test_release_deadline_cancels_and_drains_pending_request(self):
        collector = self.runtime()
        await collector._checkpoint('-1001')
        drained = asyncio.Event()

        async def release(owner):
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        self.backend.release_sources.side_effect = release
        timeout = asyncio.timeout
        with patch('collector.telegram.asyncio.timeout', side_effect=lambda _: timeout(.02)) as deadline:
            with self.assertRaises(TimeoutError):
                await collector.disconnect()
        deadline.assert_called_once_with(4)
        self.assertTrue(drained.is_set())
        self.backend.release_sources.side_effect = None
        await collector.disconnect()
        self.assertEqual(2, self.backend.release_sources.await_count)


@unittest.skipUnless(os.environ.get('TELEGRAM_TEST_DATABASE_URL'), 'requires isolated Telegram database')
class LeaseReleaseAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TelegramApiTests.asyncSetUp
    asyncTearDown = fixtures.TelegramApiTests.asyncTearDown

    async def release(self, owner=None, headers=None):
        return await self.client.post('/internal/telegram/checkpoints/release',
                                      json={'owner': str(owner or self.owner)}, headers=headers)

    async def test_multiple_sources_preserve_cursors_and_idempotence(self):
        second = uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'-1002','isolated')", second)
        result = await self.client.post('/internal/telegram/checkpoints/claim', json={'chat_id': '-1002', 'owner': str(self.owner)})
        self.assertEqual(200, result.status_code)
        await self.pool.execute('UPDATE v2_source_checkpoints SET last_message_id=35 WHERE collector_owner=$1', self.owner)
        await self.pool.execute('UPDATE v2_expert_sources SET enabled=false WHERE id=$1', second)
        before = await self.pool.fetch('SELECT * FROM v2_source_checkpoints ORDER BY source_id')
        response = await self.release()
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({'released_sources': 2}, response.json())
        after = await self.pool.fetch('SELECT *,lease_until<=clock_timestamp() AS released FROM v2_source_checkpoints ORDER BY source_id')
        for old, new in zip(before, after):
            self.assertTrue(new['released'])
            self.assertEqual({k: v for k, v in dict(old).items() if k != 'lease_until'},
                             {k: v for k, v in dict(new).items() if k not in ('lease_until', 'released')})
        self.assertEqual({'released_sources': 0}, (await self.release()).json())
        self.assertFalse(await self.pool.fetchval('SELECT enabled FROM v2_expert_sources WHERE id=$1', second))

    async def test_wrong_owner_does_not_change_any_lease(self):
        before = await self.pool.fetchrow('SELECT * FROM v2_source_checkpoints WHERE source_id=$1', self.source_id)
        response = await self.release(uuid.uuid4())
        self.assertEqual(200, response.status_code)
        self.assertEqual({'released_sources': 0}, response.json())
        self.assertEqual(dict(before), dict(await self.pool.fetchrow('SELECT * FROM v2_source_checkpoints WHERE source_id=$1', self.source_id)))

    async def test_expired_owner_release_racing_claim_cannot_release_new_owner(self):
        await self.pool.execute("UPDATE v2_source_checkpoints SET lease_until=now()-interval '1 second' WHERE source_id=$1", self.source_id)
        new_owner = uuid.uuid4()
        released, claimed = await asyncio.gather(self.release(), self.client.post('/internal/telegram/checkpoints/claim',
                                                json={'chat_id': '-1001', 'owner': str(new_owner)}))
        self.assertEqual(200, released.status_code)
        self.assertEqual({'released_sources': 0}, released.json())
        self.assertEqual(200, claimed.status_code)
        row = await self.pool.fetchrow('SELECT collector_owner,lease_until>now() AS active FROM v2_source_checkpoints WHERE source_id=$1', self.source_id)
        self.assertEqual(new_owner, row['collector_owner'])
        self.assertTrue(row['active'])
        self.assertEqual({'released_sources': 0}, (await self.release()).json())

    async def test_authentication_and_owner_validation_are_required(self):
        self.assertEqual(401, (await self.release(headers={'Authorization': 'Bearer isolated-wrong-token'})).status_code)
        self.assertEqual(422, (await self.release('not-a-uuid')).status_code)
        self.assertEqual(422, (await self.client.post('/internal/telegram/checkpoints/release', json={})).status_code)

    async def test_waiting_old_release_rechecks_owner_after_row_lock(self):
        new_owner = uuid.uuid4()
        pending = None
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("UPDATE v2_source_checkpoints SET collector_owner=$2,lease_until=now()+interval '90 seconds' WHERE source_id=$1", self.source_id, new_owner)
                    pending = asyncio.create_task(self.release())
                    async with asyncio.timeout(3):
                        while not await self.pool.fetchval("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE 'WITH released AS (UPDATE v2_source_checkpoints%')"):
                            await asyncio.sleep(.01)
            response = await asyncio.wait_for(pending, 3)
            self.assertEqual(200, response.status_code)
            self.assertEqual({'released_sources': 0}, response.json())
            row = await self.pool.fetchrow('SELECT collector_owner,lease_until>now() AS active FROM v2_source_checkpoints WHERE source_id=$1', self.source_id)
            self.assertEqual(new_owner, row['collector_owner'])
            self.assertTrue(row['active'])
        finally:
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_release_allows_immediate_handoff_without_resetting_cursor(self):
        await self.pool.execute('UPDATE v2_source_checkpoints SET last_message_id=71 WHERE source_id=$1', self.source_id)
        response = await self.release()
        self.assertEqual(200, response.status_code)
        new_owner = uuid.uuid4()
        response = await self.client.post('/internal/telegram/checkpoints/claim', json={'chat_id': '-1001', 'owner': str(new_owner)})
        self.assertEqual(200, response.status_code)
        self.assertEqual(71, response.json()['last_message_id'])
        self.assertEqual({'released_sources': 0}, (await self.release()).json())
