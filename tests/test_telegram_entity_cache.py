"""Bounded verified entity reuse must not cross sessions or hide access changes."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from collector.telegram import TelegramCollector, SourceAccessError
from test_telegram_collector import Backend, Client, CONFIG


class EntityCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=Client()
        self.client.get_entity=AsyncMock(wraps=self.client.get_entity)
        self.collector=TelegramCollector(CONFIG,backend=Backend(),client_factory=lambda *_:self.client)
        self.collector.client=self.client

    async def asyncTearDown(self):
        await self.collector.disconnect()

    async def test_repeated_group_lookup_reuses_rpc_and_other_groups_remain_distinct(self):
        first=await self.collector._entity('-1001')
        self.assertIs(first,await self.collector._entity('-1001'))
        other=await self.collector._entity('-1002')
        self.assertNotEqual(first.id,other.id)
        self.assertEqual(2,self.client.get_entity.await_count)
        first.left=True
        with self.assertRaisesRegex(SourceAccessError,'account_has_not_joined'):
            await self.collector._entity('-1001')
        self.assertEqual(2,self.client.get_entity.await_count)

    async def test_expiry_and_capacity_do_not_return_unbounded_stale_entries(self):
        with patch('collector.telegram.ENTITY_CACHE_SECONDS',0):
            await self.collector._entity('-1001')
            await self.collector._entity('-1001')
        self.assertEqual(2,self.client.get_entity.await_count)
        with patch('collector.telegram.ENTITY_CACHE_SIZE',2):
            for chat in ('-1002','-1003','-1004'):
                await self.collector._entity(chat)
        self.assertEqual(['-1003','-1004'],list(self.collector.entity_cache))

    async def test_entity_cache_survives_reconciliation_passes_but_expires_after_ten_minutes(self):
        with patch('collector.telegram.time.monotonic',side_effect=[0.0,121.0,601.0,601.0]):
            first=await self.collector._entity('-1001')
            self.assertIs(first,await self.collector._entity('-1001'))
            self.assertIsNot(first,await self.collector._entity('-1001'))
        self.assertEqual(2,self.client.get_entity.await_count)

    async def test_replacement_and_configuration_change_never_reuse_previous_client(self):
        first=await self.collector._entity('-1001')
        replacement=Client()
        replacement.get_entity=AsyncMock(wraps=replacement.get_entity)
        self.collector.client=replacement
        self.assertIsNot(first,await self.collector._entity('-1001'))
        replacement.get_entity.assert_awaited_once()
        await self.collector.apply_config(CONFIG|{'telegram_session':'different-isolated-session'})
        self.assertEqual({},self.collector.entity_cache)
        self.assertIsNone(self.collector.client)
        self.assertEqual(1,replacement.disconnect_count)

    async def test_failures_invalid_identifiers_and_disconnect_never_leave_reusable_entity(self):
        self.client.get_entity.side_effect=ConnectionError('isolated network unavailable')
        with self.assertRaises(ConnectionError):await self.collector._entity('-1001')
        self.assertFalse(self.collector.entity_cache)
        self.client.get_entity.side_effect=None
        await self.collector._entity('-1001')
        for identifier in ('0','not-a-group','-999999999999999999999'):
            with self.assertRaises(SourceAccessError):await self.collector._entity(identifier)
        self.assertEqual(2,self.client.get_entity.await_count)
        await self.collector.disconnect()
        self.assertFalse(self.collector.entity_cache)

    async def test_inflight_identity_change_cannot_cache_or_return_old_entity(self):
        entered,release=asyncio.Event(),asyncio.Event()
        original=self.client.get_entity
        async def delayed(value):
            entered.set();await release.wait();return await original(value)
        self.client.get_entity=delayed
        task=asyncio.create_task(self.collector._entity('-1001'))
        await asyncio.wait_for(entered.wait(),.5)
        self.collector.client=Client()
        release.set()
        with self.assertRaisesRegex(SourceAccessError,'session_changed_during_entity_lookup'):
            await task
        self.assertFalse(self.collector.entity_cache)
