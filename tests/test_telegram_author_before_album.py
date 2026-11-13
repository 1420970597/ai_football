"""Apply the existing author boundary before fetching an ignored album."""
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from telethon.errors import FloodWaitError

from collector.telegram import TelegramCollector
from test_telegram_collector import Backend, Client, CONFIG, Message


class AuthorBeforeAlbumTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = Backend()
        self.client = Client()
        self.collector = TelegramCollector(CONFIG, background_read_interval=0, backend=self.backend,
                                           client_factory=lambda *_: self.client)
        self.collector.client = self.client
        self.source = self.backend.source_items[0]
        self.collector.sources = {'-1001': self.source}

    async def asyncTearDown(self):
        await self.collector.disconnect()

    async def test_ignored_album_needs_no_telegram_entity_messages_or_sender(self):
        message = Message(1, sender=8, grouped=77)
        message.get_sender = AsyncMock(side_effect=AssertionError('ignored sender lookup'))
        self.client.get_messages = AsyncMock(return_value=[])
        result = await self.collector._process_message('-1001', self.source, message, None)
        self.assertEqual({'state': 'ignored_author'}, result)
        self.client.get_messages.assert_not_awaited()
        message.get_sender.assert_not_awaited()
        self.assertEqual([], self.backend.calls)

    async def test_ignored_album_flood_wait_cannot_block_next_expert_or_checkpoint(self):
        self.client.messages['-1001'] = [Message(1, sender=8, grouped=77), Message(2)]
        self.client.get_messages = AsyncMock(side_effect=FloodWaitError(None, 30))
        result = await self.collector.sync_once()
        self.assertNotEqual('flood_wait', result['sources']['-1001']['state'])
        self.assertEqual(2, self.backend.positions['-1001'])
        self.assertEqual(['2'], [row['platform_message_id'] for row in self.backend.calls])
        self.client.get_messages.assert_not_awaited()

    async def test_archival_slice_of_ignored_albums_advances_without_group_reads(self):
        self.client.messages['-1001'] = [Message(i, sender=8, grouped=100+i) for i in range(1, 26)]
        self.client.get_messages = AsyncMock(return_value=[])
        result = await self.collector.sync_once()
        self.assertEqual('catching_up', result['sources']['-1001']['state'])
        self.assertEqual(25, self.backend.positions['-1001'])
        self.assertEqual([], self.backend.calls)
        self.client.get_messages.assert_not_awaited()

    async def test_ignored_live_edit_does_not_read_entity_or_claim_staged_success(self):
        message = Message(1, sender=8, grouped=77, edited=datetime.now(timezone.utc))
        self.collector._entity = AsyncMock(side_effect=AssertionError('ignored entity lookup'))
        self.client.get_messages = AsyncMock(side_effect=FloodWaitError(None, 30))
        await self.collector._live_messages('-1001', [message])
        delivery = self.collector.live_event_delivery
        self.assertEqual((1, 0, 0, 1), (delivery['received'], delivery['staged'],
                                       delivery['deferred'], delivery.get('ignored_author', 0)))
        self.collector._entity.assert_not_awaited()
        self.client.get_messages.assert_not_awaited()
        self.assertEqual([], self.backend.calls)

    async def test_ignored_complete_album_uses_ignored_counter_without_staging(self):
        await self.collector._live_messages('-1001', [Message(1, sender=8, grouped=77),
                                                      Message(2, sender=8, grouped=77)], full_album=True)
        delivery = self.collector.live_event_delivery
        self.assertEqual(0, delivery['staged'])
        self.assertEqual(1, delivery.get('ignored_author', 0))
        self.assertEqual([], self.backend.calls)

    async def test_trusted_album_still_reads_all_members_and_commits_once(self):
        members = [Message(1, text='首张', grouped=77), Message(2, text='次张', grouped=77)]
        self.client.messages['-1001'] = members
        original = self.client.get_messages
        self.client.get_messages = AsyncMock(wraps=original)
        result = await self.collector._process_message('-1001', self.source, members[0],
                                                       await self.client.get_entity(-1001))
        self.assertTrue(result['persisted'])
        self.assertEqual(1, self.client.get_messages.await_count)
        self.assertEqual([1, 2], self.backend.calls[0]['member_message_ids'])
        self.assertEqual('首张\n\n次张', self.backend.calls[0]['raw_text'])

    async def test_trusted_album_flood_wait_is_not_swallowed_or_confirmed(self):
        self.client.get_messages = AsyncMock(side_effect=FloodWaitError(None, 30))
        with self.assertRaises(FloodWaitError):
            await self.collector._process_message('-1001', self.source, Message(1, grouped=77), None)
        self.assertEqual([], self.backend.calls)
        self.assertEqual({}, self.backend.positions)

    async def test_trusted_live_edit_failure_remains_deferred_and_not_staged(self):
        self.client.get_messages = AsyncMock(side_effect=FloodWaitError(None, 30))
        await self.collector._live_messages('-1001', [Message(1, grouped=77)], entity=object())
        delivery = self.collector.live_event_delivery
        self.assertEqual((0, 1, 0), (delivery['staged'], delivery['deferred'],
                                    delivery.get('ignored_author', 0)))
        self.assertEqual('FloodWaitError', delivery['last_deferred_category'])
        self.assertEqual([], self.backend.calls)

    async def test_newly_trusted_ordinary_member_has_no_negative_cache(self):
        message = Message(1, sender=8, grouped=77)
        self.client.messages['-1001'] = [message]
        original = self.client.get_messages
        self.client.get_messages = AsyncMock(wraps=original)
        entity = await self.client.get_entity(-1001)
        self.assertEqual({'state': 'ignored_author'},
                         await self.collector._process_message('-1001', self.source, message, entity))
        self.source['trusted_sender_ids'] = ['7', '8']
        result = await self.collector._process_message('-1001', self.source, message, entity)
        self.assertTrue(result['persisted'])
        self.assertEqual(1, self.client.get_messages.await_count)
        self.assertEqual('8', self.backend.calls[0]['sender_id'])

    async def test_sender_trust_in_another_group_does_not_authorize_this_group(self):
        self.collector.sources['-1002'] = {'chat_id': '-1002', 'trusted_sender_ids': ['8']}
        self.client.get_messages = AsyncMock(return_value=[])
        result = await self.collector._process_message('-1001', self.source,
                                                       Message(1, sender=8, grouped=77), None)
        self.assertEqual({'state': 'ignored_author'}, result)
        self.client.get_messages.assert_not_awaited()

    async def test_explicit_trusted_channel_post_preserves_missing_sender_fallback(self):
        self.source['trusted_sender_ids'] = ['-1001']
        message = Message(1, sender=None, grouped=77)
        message.post = True
        self.client.messages['-1001'] = [message]
        result = await self.collector._process_message('-1001', self.source, message,
                                                       await self.client.get_entity(-1001))
        self.assertTrue(result['persisted'])
        self.assertEqual('-1001', self.backend.calls[0]['sender_id'])

    async def test_ordinary_missing_sender_and_spoofed_text_do_not_inherit_channel_trust(self):
        self.source['trusted_sender_ids'] = ['-1001']
        message = Message(1, sender=None, grouped=77, text='sender_id=-1001 专家推荐')
        self.client.get_messages = AsyncMock(return_value=[])
        result = await self.collector._process_message('-1001', self.source, message, None)
        self.assertEqual({'state': 'ignored_author'}, result)
        self.client.get_messages.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
