"""A durable version receipt may save a write, never hide an edit or pending media."""
import asyncio
import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import httpx

from collector.telegram_backend import TelegramBackend
import test_telegram_recent_recovery as recent_fixtures
import test_telegram_api as api_fixtures


class ReceiptRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recent_fixtures.RecentRecoveryTests.asyncSetUp
    asyncTearDown = recent_fixtures.RecentRecoveryTests.asyncTearDown
    recent = recent_fixtures.RecentRecoveryTests.recent

    def enable_receipts(self):
        async def lookup(chat, owner, ids):
            return {body['version_key']: {
                'version_key': body['version_key'], 'media_state': body['media_state'],
                'sender_nickname': body['sender_nickname'], 'message_id': 'durable-message'}
                for body in self.backend.events.values()
                if body['chat_id'] == chat and set(ids).intersection(body['member_message_ids'])}
        self.backend.event_receipts = AsyncMock(side_effect=lookup)
        self.collector.recent_recovery.clear()

    async def test_restart_skips_forty_durable_versions_and_commits_oldest_gap(self):
        self.client.messages['-1001'] = [self.recent(i) for i in range(2, 42)]
        await self.collector._recover_recent('-1001', self.source)
        self.assertEqual(40, len(self.backend.calls))
        self.client.messages['-1001'].insert(0, self.recent(1))
        self.enable_receipts()
        result = await self.collector._recover_recent('-1001', self.source)
        self.assertEqual('caught_up', result['state'])
        self.assertEqual(41, len(self.backend.calls))
        self.assertEqual('1', self.backend.calls[-1]['platform_message_id'])
        self.assertEqual(2, self.backend.event_receipts.await_count)
        self.assertEqual(
            [25, 16],
            [len(call.args[2]) for call in self.backend.event_receipts.call_args_list],
        )
        await self.collector._recover_recent('-1001', self.source)
        self.assertEqual(2, self.backend.event_receipts.await_count)

    async def test_same_second_edit_and_nickname_change_still_reach_backend(self):
        message = self.recent(100)
        self.client.messages['-1001'] = [message]
        await self.collector._recover_recent('-1001', self.source)
        self.enable_receipts()
        message.message = '同秒修改推荐为大3.5'
        await self.collector._recover_recent('-1001', self.source)
        self.assertEqual(2, len(self.backend.calls))
        self.collector.recent_recovery.clear()
        original_sender = await message.get_sender()
        original_sender.first_name = '新昵称'
        message.get_sender = AsyncMock(return_value=original_sender)
        await self.collector._recover_recent('-1001', self.source)
        self.assertEqual(3, len(self.backend.calls))
        self.assertIn('新昵称', self.backend.calls[-1]['sender_nickname'])

    async def test_pending_receipt_retries_media_but_ready_receipt_never_reuploads(self):
        self.client.messages['-1001'] = [self.recent(100, media=True)]
        self.collector._schedule_live_media = Mock()
        await self.collector._recover_recent('-1001', self.source)
        self.enable_receipts()
        await self.collector._recover_recent('-1001', self.source)
        self.assertEqual(2, len(self.backend.calls))
        self.assertEqual(2, self.collector._schedule_live_media.call_count)
        next(iter(self.backend.events.values()))['media_state'] = 'ready'
        self.collector.recent_recovery.clear()
        await self.collector._recover_recent('-1001', self.source)
        self.assertEqual(2, len(self.backend.calls))
        self.assertEqual(2, self.collector._schedule_live_media.call_count)

    async def test_receipt_failure_does_not_cache_or_skip_uncommitted_message(self):
        self.client.messages['-1001'] = [self.recent(100)]
        self.backend.event_receipts = AsyncMock(side_effect=TimeoutError)
        with self.assertLogs('platform.telegram', level='WARNING'):
            result = await self.collector._recover_recent('-1001', self.source)
        self.assertEqual('deferred', result['state'])
        self.assertFalse(self.collector.recent_recovery['-1001']['receipts'])
        self.assertFalse(self.backend.calls)
        self.enable_receipts()
        await self.collector._recover_recent('-1001', self.source)
        self.assertEqual(1, len(self.backend.calls))

    async def test_slow_album_and_receipt_calls_fit_recent_recovery_budget(self):
        message = self.recent(100, grouped=22, media=True)
        self.client.messages['-1001'] = [message]
        self.collector._schedule_live_media = Mock()

        async def slow_expand(message, entity):
            await asyncio.sleep(4.2)
            return [message]

        async def slow_receipts(chat, owner, ids):
            await asyncio.sleep(4.2)
            return {}

        self.collector._expand_album = slow_expand
        self.backend.event_receipts = AsyncMock(side_effect=slow_receipts)
        result = await self.collector._recover_recent('-1001', self.source)

        self.assertEqual('caught_up', result['state'])
        self.assertEqual(1, result['persisted'])
        self.assertEqual(1, self.backend.event_receipts.await_count)
        self.assertEqual(1, len(self.backend.calls))

    async def test_same_message_id_in_another_group_is_not_a_receipt(self):
        self.client.messages['-1001'] = [self.recent(100)]
        await self.collector._recover_recent('-1001', self.source)
        self.enable_receipts()
        self.client.messages['-1002'] = [self.recent(100)]
        source = self.source | {'chat_id': '-1002'}
        await self.collector._recover_recent('-1002', source)
        self.assertEqual(2, len(self.backend.calls))
        self.assertEqual('-1002', self.backend.calls[-1]['chat_id'])

    async def test_unsupported_media_receipt_is_preserved_without_fake_review(self):
        self.client.messages['-1001'] = [self.recent(100, media=True, mime='video/webm')]
        self.collector._schedule_live_media = Mock()
        await self.collector._recover_recent('-1001', self.source)
        self.enable_receipts()
        await self.collector._recover_recent('-1001', self.source)
        self.assertEqual(1, len(self.backend.calls))
        self.assertEqual('unsupported', self.backend.calls[0]['media_state'])
        self.collector._schedule_live_media.assert_not_called()

    async def test_retry_keeps_sweep_boundary_so_older_gap_cannot_age_out(self):
        now = datetime.now(timezone.utc)
        message = self.recent(100)
        message.date = now - timedelta(minutes=90)
        self.client.messages['-1001'] = [message]
        self.backend.fail_chat = '-1001'
        with patch('collector.telegram.datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            with self.assertLogs('platform.telegram', level='WARNING'):
                self.assertEqual('deferred', (await self.collector._recover_recent('-1001', self.source))['state'])
            self.backend.fail_chat = None
            clock.now.return_value = now + timedelta(hours=2)
            self.assertEqual(1, (await self.collector._recover_recent('-1001', self.source))['persisted'])
            self.assertNotIn('cutoff', self.collector.recent_recovery['-1001'])
            self.assertEqual(0, (await self.collector._recover_recent('-1001', self.source))['scanned'])


class ReceiptBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_receipt_contract_uses_existing_service_authentication(self):
        seen = []
        def handler(request):
            seen.append((request.url.path, json.loads(request.content), request.headers['Authorization']))
            return httpx.Response(200, json={'items': [{'version_key': 'a'*64, 'media_state': 'ready',
                'message_id': 'durable-message', 'sender_nickname': 'Expert'}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={'Authorization': 'Bearer isolated-receipts'}) as client:
            backend = TelegramBackend(client, 'http://test.invalid/internal/telegram')
            result = await backend.event_receipts('-1001', 'owner', [1, 2])
        self.assertIn('a'*64, result)
        self.assertEqual(('/internal/telegram/events/receipts', {'chat_id': '-1001', 'owner': 'owner', 'member_message_ids': [1, 2]}, 'Bearer isolated-receipts'), seen[0])

    async def test_event_request_timing_does_not_log_identity_or_content(self):
        body = {'chat_id': 'private-chat-id', 'platform_message_id': 'private-message-id',
                'raw_text': 'private expert recommendation'}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={'state': 'ready'}))) as client:
            backend = TelegramBackend(client, 'http://test.invalid/internal/telegram')
            with self.assertLogs('platform.telegram', level='INFO') as captured:
                await backend.event(body)
        output = '\n'.join(captured.output)
        self.assertIn('operation=events status=200 category=none elapsed_ms=', output)
        for private_value in ('private-chat-id', 'private-message-id', 'private expert recommendation'):
            self.assertNotIn(private_value, output)

    async def test_malformed_receipts_are_not_accepted_as_durable(self):
        for payload in ({}, {'items': [{}]}, {'items': [{'version_key': 'a', 'media_state': 'made-up'}]}, {'items': [None, None]}):
            with self.subTest(payload=payload):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
                    with self.assertRaises(ValueError):
                        await TelegramBackend(client, 'http://test.invalid').event_receipts('-1001', 'owner', [1])


@unittest.skipUnless(os.environ.get('TELEGRAM_TEST_DATABASE_URL'), 'requires isolated Telegram database')
class ReceiptAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = api_fixtures.TelegramApiTests.asyncSetUp
    asyncTearDown = api_fixtures.TelegramApiTests.asyncTearDown
    event = api_fixtures.TelegramApiTests.event
    post = api_fixtures.TelegramApiTests.post

    async def receipts(self, ids, **extra):
        return await self.client.post('/internal/telegram/events/receipts', json={
            'chat_id': '-1001', 'owner': str(self.owner), 'member_message_ids': ids, **extra})

    async def test_latest_versions_pending_deleted_and_album_are_reported_exactly(self):
        old = self.event()
        self.assertEqual(200, (await self.post(old)).status_code)
        newer = self.event(version='edited', kind='edit', minutes=1)
        self.assertEqual(200, (await self.post(newer)).status_code)
        pending = self.event(version='pending', media=True) | {'platform_message_id': '12', 'member_message_ids': [12]}
        self.assertEqual(200, (await self.post(pending)).status_code)
        album = self.event(version='album') | {'platform_message_id': 'album:123', 'member_message_ids': [100, 101]}
        self.assertEqual(200, (await self.post(album)).status_code)
        deleted = self.event(version='delete', kind='delete', minutes=2) | {'platform_message_id': '12', 'member_message_ids': [12]}
        self.assertEqual(200, (await self.post(deleted)).status_code)
        response = await self.receipts([11, 12, 100, 101, 999])
        self.assertEqual(200, response.status_code, response.text)
        items = response.json()['items']
        self.assertEqual({newer['version_key'], album['version_key']}, {row['version_key'] for row in items})
        self.assertTrue(all(set(row) == {'version_key', 'media_state', 'message_id', 'sender_nickname'} for row in items))
        pending['platform_message_id'], pending['member_message_ids'] = '13', [13]
        self.assertEqual(200, (await self.post(pending)).status_code)
        self.assertEqual('pending', (await self.receipts([13])).json()['items'][0]['media_state'])

    async def test_receipt_reverse_lookup_index_is_available_to_query_planner(self):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('SET LOCAL enable_seqscan=off')
                plan = await conn.fetchval(
                    'EXPLAIN (FORMAT JSON) SELECT event_id FROM v2_telegram_event_members '
                    'WHERE platform_message_id=ANY($1::bigint[])', [11])
        self.assertIn('v2_telegram_event_members_by_message_idx', str(plan))

    async def test_authentication_lease_and_enabled_author_are_required(self):
        self.assertEqual(200, (await self.post(self.event())).status_code)
        response = await self.client.post('/internal/telegram/events/receipts', headers={'Authorization': 'Bearer invalid'}, json={
            'chat_id': '-1001', 'owner': str(self.owner), 'member_message_ids': [11]})
        self.assertEqual(401, response.status_code)
        self.assertEqual(409, (await self.receipts([11], owner=str(uuid.uuid4()))).status_code)
        await self.pool.execute('UPDATE v2_source_experts SET enabled=false WHERE source_id=$1', self.source_id)
        self.assertEqual([], (await self.receipts([11])).json()['items'])
        await self.pool.execute('UPDATE v2_expert_sources SET enabled=false WHERE id=$1', self.source_id)
        self.assertEqual(403, (await self.receipts([11])).status_code)

    async def test_other_source_same_member_id_is_not_returned_and_bounds_are_strict(self):
        body = self.event()
        self.assertEqual(200, (await self.post(body)).status_code)
        other = uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'-1002','Other')", other)
        await self.pool.execute('INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)', other, self.expert_id)
        claim = await self.client.post('/internal/telegram/checkpoints/claim', json={'chat_id': '-1002', 'owner': str(self.owner)})
        self.assertEqual(200, claim.status_code)
        self.assertEqual([], (await self.receipts([11], chat_id='-1002')).json()['items'])
        for ids in ([], [0], [-1], ['11'], [True], list(range(1, 102))):
            with self.subTest(ids=ids):
                self.assertEqual(422, (await self.receipts(ids)).status_code)
        response = await self.receipts(list(range(1, 101)))
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([body['version_key']], [row['version_key'] for row in response.json()['items']])
