import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon.errors import AuthKeyDuplicatedError

from collector.telegram import TelegramCollector
import test_telegram_collector as fixtures


class ReplyLookupFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = fixtures.Backend()
        self.collector = TelegramCollector(fixtures.CONFIG, backend=self.backend)
        self.source = self.backend.source_items[0]
        self.collector.sources = {'-1001': self.source}
        self.message = fixtures.Message(105, text='半场大0.75')
        self.message.reply_to_msg_id = 44
        self.reply = SimpleNamespace(id=44, sender_id=8, message='比赛原始上下文', media=None)
        self.message.get_reply_message = AsyncMock(return_value=self.reply)

    async def asyncTearDown(self):
        await self.collector.disconnect()

    async def stage(self):
        return await self.collector._stage_group('-1001', self.source, [self.message])

    async def test_transient_failure_does_not_supersede_complete_version_and_recovery_deduplicates(self):
        await self.stage()
        original = self.backend.calls[0]['version_key']
        self.message.get_reply_message.side_effect = ConnectionError('isolated reply lookup failure')
        with self.assertRaises(ConnectionError):
            await self.stage()
        self.assertEqual(1, len(self.backend.events))
        self.assertEqual(1, len(self.backend.calls))
        self.assertEqual({}, self.backend.positions)
        self.message.get_reply_message.side_effect = None
        await self.stage()
        self.assertEqual(1, len(self.backend.events))
        self.assertEqual(original, self.backend.calls[-1]['version_key'])

    async def test_first_failure_is_deferred_and_successful_retry_is_not_dropped(self):
        self.message.get_reply_message.side_effect = ConnectionError('isolated reply lookup failure')
        await self.collector._live_messages('-1001', [self.message])
        self.assertEqual({}, self.backend.events)
        self.assertEqual({}, self.backend.positions)
        self.assertEqual(1, self.collector.live_event_delivery['deferred'])
        self.assertEqual(0, self.collector.live_event_delivery['staged'])
        self.message.get_reply_message.side_effect = None
        await self.collector._live_messages('-1001', [self.message])
        self.assertEqual(1, len(self.backend.events))
        self.assertEqual(1, self.collector.live_event_delivery['staged'])

    async def test_reply_child_cancellation_propagates_without_event(self):
        self.message.get_reply_message.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.stage()
        self.assertEqual([], self.backend.calls)

    async def test_successful_missing_reply_is_still_explicit_unavailable(self):
        self.message.get_reply_message.return_value = None
        result, payload = await self.stage()
        self.assertTrue(result['persisted'])
        self.assertIsNone(payload)
        self.assertEqual({'unavailable': True}, self.backend.calls[0]['reply_context'])

    async def test_synchronous_reply_setup_error_is_not_content(self):
        tasks = []
        create_task = asyncio.create_task
        def track(coro):
            task = create_task(coro)
            tasks.append(task)
            return task
        def unavailable():
            raise RuntimeError('isolated synchronous reply lookup failure')
        self.message.get_reply_message = unavailable
        with patch('collector.telegram.asyncio.create_task', side_effect=track):
            with self.assertRaises(RuntimeError):
                await self.stage()
        self.assertTrue(tasks)
        self.assertTrue(all(task.done() for task in tasks))
        self.assertEqual([], self.backend.calls)

    async def test_duplicate_auth_key_uses_existing_session_invalidation_path(self):
        self.message.get_reply_message.side_effect = AuthKeyDuplicatedError(None)
        self.collector._invalidate_active_session = AsyncMock()
        await self.collector._live_messages('-1001', [self.message])
        self.collector._invalidate_active_session.assert_awaited_once()
        self.assertEqual([], self.backend.calls)
        self.assertEqual('AuthKeyDuplicatedError', self.collector.live_event_delivery['last_deferred_category'])

    async def test_parallel_sender_lookup_drains_before_reply_error_propagates(self):
        completed = asyncio.Event()
        async def sender():
            await asyncio.sleep(0)
            completed.set()
            return SimpleNamespace(id=7, first_name='专家', last_name='', username=None)
        self.message.get_sender = sender
        self.message.get_reply_message.side_effect = ConnectionError('isolated reply lookup failure')
        with self.assertRaises(ConnectionError):
            await self.stage()
        self.assertTrue(completed.is_set())
        self.assertEqual([], self.backend.calls)

    async def test_album_reference_uses_member_that_actually_replied(self):
        first = fixtures.Message(104, text='同一相册', grouped=77)
        first.get_reply_message = AsyncMock(return_value=None)
        self.message.grouped_id = 77
        result, _ = await self.collector._stage_group('-1001', self.source, [first, self.message])
        self.assertTrue(result['persisted'])
        self.assertEqual('比赛原始上下文', self.backend.calls[0]['reply_context']['text'])
        first.get_reply_message.assert_not_awaited()
        self.message.get_reply_message.assert_awaited_once()
