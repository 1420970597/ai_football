"""Bounded lease recovery without extending ambiguous-write retries."""
import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch
import uuid

import httpx

from collector.telegram_backend import TelegramBackend
import test_telegram_api as fixtures


class LeaseConnectRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_connect_timeout_retries_identical_claim_and_logs_no_identity(self):
        seen = []

        def handler(request):
            seen.append((request.method, request.url.path, request.content))
            if len(seen) == 1:
                raise httpx.ConnectTimeout('private connection detail', request=request)
            return httpx.Response(200, json={'last_message_id': 35, 'lease_until': 'future'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid/internal/telegram')
            with patch('collector.telegram_backend.asyncio.sleep', new_callable=AsyncMock) as pause:
                with self.assertLogs('platform.telegram', level='WARNING') as logs:
                    result = await backend.checkpoint('private-chat', 'private-owner')
            self.assertFalse(backend._source_locks['private-chat'].locked())
        self.assertEqual(35, result['last_message_id'])
        self.assertEqual(2, len(seen))
        self.assertEqual(seen[0], seen[1])
        pause.assert_awaited_once_with(.5)
        output = '\n'.join(logs.output)
        self.assertIn('operation=/checkpoints/claim category=ConnectTimeout attempt=1', output)
        for private in ('private-chat', 'private-owner', 'private connection detail'):
            self.assertNotIn(private, output)

    async def test_second_connect_timeout_propagates_after_exactly_one_retry(self):
        seen = []

        def handler(request):
            seen.append(request)
            raise httpx.ConnectTimeout('unavailable', request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch('collector.telegram_backend.asyncio.sleep', new_callable=AsyncMock) as pause:
                with self.assertLogs('platform.telegram', level='WARNING'):
                    with self.assertRaises(httpx.ConnectTimeout):
                        await TelegramBackend(client, 'http://isolated.invalid').checkpoint('chat', 'owner')
        self.assertEqual(2, len(seen))
        pause.assert_awaited_once_with(.5)

    async def test_other_failures_and_write_paths_do_not_gain_transport_retries(self):
        cases = [(kind, 'POST', '/checkpoints/claim') for kind in
                 (httpx.ReadTimeout, httpx.PoolTimeout, httpx.ConnectError)]
        cases += [(kind, 'POST', '/events') for kind in
                  (httpx.ReadTimeout, httpx.PoolTimeout, httpx.ConnectError)]
        cases += [(httpx.ConnectTimeout, 'POST', '/directory'),
                  (httpx.ConnectTimeout, 'PUT', '/checkpoints')]
        for error, method, path in cases:
            with self.subTest(error=error.__name__, method=method, path=path):
                seen = []

                def handler(request):
                    seen.append(request)
                    raise error('unavailable', request=request)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with patch('collector.telegram_backend.asyncio.sleep', new_callable=AsyncMock) as pause:
                        with self.assertRaises(error):
                            await TelegramBackend(client, 'http://isolated.invalid')._call(
                                method, path, {'chat_id': 'chat', 'owner': 'owner'})
                    pause.assert_not_awaited()
                self.assertEqual(1, len(seen))

    async def test_outer_deadline_cancels_backoff_and_releases_source_lock(self):
        seen = []

        def handler(request):
            seen.append(request)
            raise httpx.ConnectTimeout('unavailable', request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = TelegramBackend(client, 'http://isolated.invalid')
            with self.assertLogs('platform.telegram', level='WARNING'):
                with self.assertRaises(TimeoutError):
                    async with asyncio.timeout(.02):
                        await backend.checkpoint('chat', 'owner')
            self.assertFalse(backend._source_locks['chat'].locked())
        self.assertEqual(1, len(seen))


@unittest.skipUnless(os.environ.get('TELEGRAM_TEST_DATABASE_URL'), 'requires isolated Telegram database')
class LeaseConnectRecoveryAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TelegramApiTests.asyncSetUp
    asyncTearDown = fixtures.TelegramApiTests.asyncTearDown

    async def test_recovered_renewal_keeps_durable_cursor_and_does_not_transfer_owner(self):
        body = {'chat_id': '-1001', 'owner': str(self.owner), 'last_message_id': 35, 'previous_message_id': 0}
        self.assertEqual(200, (await self.client.put('/internal/telegram/checkpoints', json=body)).status_code)
        seen = []

        async def handler(request):
            seen.append(request.content)
            if len(seen) == 1:
                raise httpx.ConnectTimeout('isolated failure before sending', request=request)
            return await self.client.request(request.method, request.url.path, json=json.loads(request.content))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch('collector.telegram_backend.asyncio.sleep', new_callable=AsyncMock):
                with self.assertLogs('platform.telegram', level='WARNING'):
                    result = await TelegramBackend(client, 'http://isolated.invalid/internal/telegram').checkpoint(
                        '-1001', str(self.owner))
        self.assertEqual([seen[0], seen[0]], seen)
        self.assertEqual(35, result['last_message_id'])
        row = await self.pool.fetchrow('SELECT collector_owner,last_message_id FROM v2_source_checkpoints WHERE source_id=$1', self.source_id)
        self.assertEqual(self.owner, row['collector_owner'])
        self.assertEqual(35, row['last_message_id'])
        conflict = await self.client.post('/internal/telegram/checkpoints/claim', json={'chat_id': '-1001', 'owner': str(uuid.uuid4())})
        self.assertEqual(409, conflict.status_code)
