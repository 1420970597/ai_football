import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from test_telegram_detection_timing import CollectorDetectionTests
from test_telegram_collector import Message,Client


class AlbumFallbackTests(CollectorDetectionTests):
    async def test_missing_album_callback_fetches_full_group_once(self):
        c,b=self.make_collector();c.client=Client()
        one=Message(11,grouped=90,media=True);two=Message(12,text='',grouped=90,media=True)
        c.client.messages['-1001']=[one,two]
        entity=await c.client.get_entity('-1001')
        c._live_messages=AsyncMock()
        c._schedule_album_fallback('-1001',one,entity)
        c._schedule_album_fallback('-1001',two,entity)
        self.assertEqual(1,len(c.live_album_tasks))
        await asyncio.gather(*tuple(c.live_album_tasks.values()))
        c._live_messages.assert_awaited_once()
        self.assertEqual([11,12],[m.id for m in c._live_messages.call_args.args[1]])
        await c.disconnect()

    async def test_real_album_cancels_fallback_and_disconnect_drains(self):
        c,b=self.make_collector();one=Message(11,grouped=90,media=True)
        c._live_messages=AsyncMock();c._schedule_album_fallback('-1001',one,None)
        task=next(iter(c.live_album_tasks.values()))
        await c._on_album(SimpleNamespace(chat_id=-1001,messages=[one]))
        self.assertTrue(task.cancelled());c._live_messages.assert_awaited_once()
        c._schedule_album_fallback('-1001',one,None)
        await c.disconnect()
        self.assertFalse(c.live_album_tasks)
