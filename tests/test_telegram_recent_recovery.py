"""Recent gaps must not wait behind years of archival history."""
import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, Mock, patch

from collector.telegram import TelegramCollector
from test_telegram_collector import Backend, Client, CONFIG, Message


class RecentRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = Backend()
        self.client = Client()
        self.collector = TelegramCollector(CONFIG, backend=self.backend, client_factory=lambda *_: self.client)
        self.collector.client = self.client
        self.source = self.backend.source_items[0]
        self.collector.sources = {'-1001': self.source}

    async def asyncTearDown(self):
        await self.collector.disconnect()

    def recent(self, identifier, **kwargs):
        message = Message(identifier, **kwargs)
        message.date = datetime.now(timezone.utc) - timedelta(minutes=2)
        return message

    async def test_latest_gap_retries_failed_commit_without_moving_archive_checkpoint(self):
        self.backend.positions['-1001'] = 1
        self.client.messages['-1001'] = [Message(1), self.recent(1_400_000)]
        self.backend.fail_chat = '-1001'
        with self.assertLogs('platform.telegram', level='WARNING'):
            failed = await self.collector._recover_recent('-1001', self.source)
        self.assertEqual('deferred', failed['state'])
        self.assertFalse(self.collector.recent_recovery['-1001']['receipts'])
        self.backend.fail_chat = None
        recovered = await self.collector._recover_recent('-1001', self.source)
        self.assertEqual('caught_up', recovered['state'])
        self.assertEqual(['1400000'], [item['platform_message_id'] for item in self.backend.calls])
        self.assertEqual(1, self.backend.positions['-1001'])
        await self.collector._recover_recent('-1001', self.source)
        self.assertEqual(1, len(self.backend.calls))

    async def test_pages_continue_past_head_and_new_message_precedes_older_work(self):
        self.client.messages['-1001'] = [self.recent(i) for i in range(1, 13)]
        with patch('collector.telegram.RECENT_HISTORY_PAGE_SIZE', 3):
            first = await self.collector._recover_recent('-1001', self.source)
            self.assertEqual('catching_up', first['state'])
            self.assertEqual([12, 11, 10, 9, 8, 7], [int(item['platform_message_id']) for item in self.backend.calls])
            self.client.messages['-1001'].append(self.recent(13))
            await self.collector._recover_recent('-1001', self.source)
            self.assertEqual('13', self.backend.calls[6]['platform_message_id'])
            last = await self.collector._recover_recent('-1001', self.source)
            self.assertEqual('catching_up', last['state'])
            last = await self.collector._recover_recent('-1001', self.source)
        self.assertEqual('caught_up', last['state'])
        self.assertEqual(set(range(1, 14)), {int(item['platform_message_id']) for item in self.backend.calls})
        self.assertEqual(13, len(self.backend.events))

    async def test_cancelled_unknown_commit_is_not_cached_and_retries(self):
        self.client.messages['-1001'] = [self.recent(100)]
        entered = asyncio.Event()
        original = self.backend.event
        async def blocked(_):
            entered.set()
            await asyncio.Future()
        self.backend.event = blocked
        task = asyncio.create_task(self.collector._recover_recent('-1001', self.source))
        await asyncio.wait_for(entered.wait(), .5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.collector.recent_recovery['-1001']['receipts'])
        self.backend.event = original
        result = await self.collector._recover_recent('-1001', self.source)
        self.assertEqual(1, result['persisted'])

    async def test_restart_replays_durable_version_without_duplicate_event(self):
        self.client.messages['-1001'] = [self.recent(100)]
        await self.collector._recover_recent('-1001', self.source)
        restarted = TelegramCollector(CONFIG, backend=self.backend, client_factory=lambda *_: self.client)
        restarted.client = self.client
        await restarted._recover_recent('-1001', self.source)
        self.assertEqual(2, len(self.backend.calls))
        self.assertEqual(1, len(self.backend.events))
        await restarted.disconnect()

    async def test_album_is_complete_and_media_scheduled_only_after_commit(self):
        self.client.messages['-1001'] = [self.recent(i, grouped=22, media=True) for i in [100, 101]]
        self.collector._schedule_live_media = Mock()
        result = await self.collector._recover_recent('-1001', self.source)
        self.assertEqual(1, result['persisted'])
        self.assertEqual([100, 101], self.backend.calls[0]['member_message_ids'])
        self.assertEqual('pending', self.backend.calls[0]['media_state'])
        self.collector._schedule_live_media.assert_called_once()

    async def test_receipt_cache_is_bounded_and_edit_is_not_hidden(self):
        self.client.messages['-1001'] = [self.recent(i) for i in [100, 101, 102]]
        with patch('collector.telegram.RECENT_HISTORY_RECEIPTS', 2):
            await self.collector._recover_recent('-1001', self.source)
            self.assertEqual(2, len(self.collector.recent_recovery['-1001']['receipts']))
        self.client.messages['-1001'][0].message = '相同秒内修改的推荐'
        await self.collector._recover_recent('-1001', self.source)
        self.assertEqual(4, len(self.backend.events))

    async def test_recent_failure_is_visible_in_source_health(self):
        self.collector._recover_recent = AsyncMock(return_value={'state': 'deferred', 'reason': 'TimeoutError'})
        result = await self.collector._sync_source('-1001', self.source)
        self.assertEqual('recent_deferred', result['state'])
        self.assertEqual('TimeoutError', result['recent_history']['reason'])

    async def test_recent_source_failure_does_not_block_another_group(self):
        self.backend.source_items.append(self.source | {'chat_id': '-1002'})
        self.client.messages = {chat: [self.recent(100)] for chat in ['-1001', '-1002']}
        self.backend.fail_chat = '-1002'
        with self.assertLogs('platform.telegram', level='WARNING'):
            result = await self.collector.sync_once()
        self.assertEqual('caught_up', result['sources']['-1001']['state'])
        self.assertEqual('unavailable', result['sources']['-1002']['state'])
        self.assertEqual({'-1001'}, {event['chat_id'] for event in self.backend.calls})


if __name__ == '__main__':
    unittest.main()
