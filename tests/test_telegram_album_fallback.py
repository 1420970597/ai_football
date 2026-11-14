import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from test_telegram_detection_timing import CollectorDetectionTests
from test_telegram_collector import Message,Client
from test_telegram_channel_identity import ChannelIdentityCollectorTests
from collector.telegram import events
import time


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


class SignedAlbumFallbackTests(ChannelIdentityCollectorTests):
    async def test_signed_broadcast_callback_uses_verified_event_or_session_cache(self):
        m=self.message(11,sender=77);m.grouped_id=91
        class Event:
            message=m
            chat_id=self.chat_id
        c=self.collector;c._schedule_album_fallback=Mock()
        for via_cache in (False,True):
            event=Event()
            if via_cache:c.entity_cache[self.chat_id]=(c.client,time.monotonic()+30,self.entity)
            else:event.chat=self.entity
            with patch('collector.telegram.events.NewMessage.Event',Event):await c._on_message(event)
            c._schedule_album_fallback.assert_called_with(self.chat_id,m,self.entity)
        self.assertEqual(1,len(c.detection_times))
        self.assertNotIn('verified_broadcast',self.source)

    async def test_group_or_expired_session_metadata_cannot_trust_signed_author(self):
        m=self.message(11,sender=77);m.grouped_id=91
        class Event:
            message=m
            chat_id=self.chat_id
        c=self.collector;c._schedule_album_fallback=Mock()
        c.entity_cache[self.chat_id]=(c.client,time.monotonic()-1,self.entity)
        with patch('collector.telegram.events.NewMessage.Event',Event):await c._on_message(Event())
        c._schedule_album_fallback.assert_not_called()
        self.entity.megagroup=True;event=Event();event.chat=self.entity
        with patch('collector.telegram.events.NewMessage.Event',Event):await c._on_message(event)
        c._schedule_album_fallback.assert_not_called()
