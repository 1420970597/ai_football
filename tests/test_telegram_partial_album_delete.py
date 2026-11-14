import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from collector.telegram import TelegramCollector
from test_telegram_collector import CONFIG,Backend,Client,Message


class PartialAlbumDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend=Backend();self.client=Client()
        self.collector=TelegramCollector(CONFIG,background_read_interval=0,backend=self.backend,client_factory=lambda *_:self.client)
        self.collector.client=self.client
        self.collector.sources={'-1001':self.backend.source_items[0]}

    async def asyncTearDown(self):
        await self.collector.disconnect()

    async def test_survivors_are_refetched_and_restaged_without_deleted_member(self):
        survivor=Message(11,media=True,grouped=55)
        self.collector._delete=AsyncMock(return_value={'state':'deleted','surviving_member_ids':[11]})
        self.collector._entity=AsyncMock(return_value='verified-entity')
        self.client.get_messages=AsyncMock(return_value=[survivor,None])
        self.collector._live_messages=AsyncMock()
        await self.collector._on_delete(SimpleNamespace(chat_id=-1001,deleted_ids=[12]))
        self.collector._delete.assert_awaited_once_with('-1001',[12])
        self.client.get_messages.assert_awaited_once_with('verified-entity',ids=[11])
        self.collector._live_messages.assert_awaited_once_with('-1001',[survivor],entity='verified-entity')

    async def test_normal_delete_does_not_fetch_or_resurrect(self):
        self.collector._delete=AsyncMock(return_value={'state':'deleted','surviving_member_ids':[]})
        self.collector._entity=AsyncMock()
        self.collector._live_messages=AsyncMock()
        await self.collector._on_delete(SimpleNamespace(chat_id=-1001,deleted_ids=[12]))
        self.collector._entity.assert_not_awaited()
        self.collector._live_messages.assert_not_awaited()
