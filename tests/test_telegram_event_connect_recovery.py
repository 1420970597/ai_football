"""Event retries preserve the original body, message ordering and durable dedupe."""
import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from collector.telegram_backend import TelegramBackend
import test_telegram_api as fixtures


class EventConnectRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_connect_failure_retries_identical_media_body_without_logging_content(self):
        body = {'chat_id': 'private-chat', 'platform_message_id': '19', 'raw_text': 'private-text',
                'media_objects': [{'object_key': 'private-object', 'sha256': 'a' * 64}]}
        seen = []

        def handler(request):
            seen.append(request.content)
            if len(seen) == 1:
                raise httpx.ConnectTimeout('private-network', request=request)
            return httpx.Response(200, json={'persisted': True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid')
            with patch('collector.telegram_backend.asyncio.sleep', new_callable=AsyncMock) as pause:
                with self.assertLogs('platform.telegram', level='WARNING') as logs:
                    result = await backend._call('POST', '/events', body)
            self.assertEqual({}, backend._event_locks)
        self.assertEqual({'persisted': True}, result)
        self.assertEqual(2, len(seen))
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(body, json.loads(seen[0]))
        pause.assert_awaited_once_with(.5)
        output = '\n'.join(logs.output)
        self.assertIn('operation=/events category=ConnectTimeout attempt=1', output)
        for value in ('private-chat', 'private-text', 'private-object', 'private-network'):
            self.assertNotIn(value, output)

    async def test_second_connect_failure_is_visible_and_lock_released(self):
        seen = []

        def handler(request):
            seen.append(request.content)
            raise httpx.ConnectTimeout('unavailable', request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid')
            with patch('collector.telegram_backend.asyncio.sleep', new_callable=AsyncMock) as pause:
                with self.assertRaises(httpx.ConnectTimeout):
                    await backend._call('POST', '/events', {'chat_id': 'chat', 'platform_message_id': '19'})
            self.assertEqual({}, backend._event_locks)
        self.assertEqual(2, len(seen))
        pause.assert_awaited_once_with(.5)

    async def test_outer_deadline_cancels_backoff_and_releases_event_lock(self):
        seen = []

        def handler(request):
            seen.append(request.content)
            raise httpx.ConnectTimeout('unavailable', request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid')
            with self.assertRaises(TimeoutError):
                async with asyncio.timeout(.02):
                    await backend._call('POST', '/events', {'chat_id': 'chat', 'platform_message_id': '19'})
            self.assertEqual({}, backend._event_locks)
        self.assertEqual(1, len(seen))

    async def test_retry_does_not_allow_newer_message_version_to_overtake(self):
        real_sleep = asyncio.sleep
        entered = asyncio.Event()
        release = asyncio.Event()
        seen = []

        async def handler(request):
            version = json.loads(request.content)['version']
            seen.append(version)
            if len(seen) == 1:
                raise httpx.ConnectTimeout('unavailable', request=request)
            return httpx.Response(200, json={'version': version})

        async def backoff(_):
            entered.set()
            await release.wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid')
            base = {'chat_id': 'chat', 'platform_message_id': '19'}
            with patch('collector.telegram_backend.asyncio.sleep', side_effect=backoff):
                first = asyncio.create_task(backend._call('POST', '/events', base | {'version': 1}))
                await asyncio.wait_for(entered.wait(), 1)
                second = asyncio.create_task(backend._call('POST', '/events', base | {'version': 2}))
                try:
                    await real_sleep(0)
                    self.assertEqual([1], seen)
                finally:
                    release.set()
                self.assertEqual([{'version': 1}, {'version': 2}], await asyncio.gather(first, second))
            self.assertEqual({}, backend._event_locks)
        self.assertEqual([1, 1, 2], seen)


@unittest.skipUnless(os.environ.get('TELEGRAM_TEST_DATABASE_URL'), 'requires isolated Telegram database')
class EventConnectRecoveryAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TelegramApiTests.asyncSetUp
    asyncTearDown = fixtures.TelegramApiTests.asyncTearDown
    event = fixtures.TelegramApiTests.event

    async def test_connect_recovery_and_duplicate_delivery_create_one_message_and_one_job(self):
        seen = []
        body = self.event()

        async def handler(request):
            seen.append(request.content)
            if len(seen) == 1:
                raise httpx.ConnectTimeout('before sending', request=request)
            return await self.client.post('/internal/telegram/events', json=json.loads(request.content))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid')
            with patch('collector.telegram_backend.asyncio.sleep', new_callable=AsyncMock):
                recovered = await backend._call('POST', '/events', body)
            repeated = await backend._call('POST', '/events', body)
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(recovered['message_id'], repeated['message_id'])
        self.assertEqual(recovered['review_id'], str(await self.pool.fetchval(
            'SELECT id FROM v2_analysis_jobs')))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_messages'))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_jobs'))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_telegram_events'))
        self.assertEqual(0, await self.pool.fetchval('SELECT last_message_id FROM v2_source_checkpoints'))
