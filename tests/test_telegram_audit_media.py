import base64
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from test_telegram_channel_identity import ChannelIdentityCollectorTests


class AuditMediaTests(ChannelIdentityCollectorTests):
    async def test_private_media_requires_trusted_nonexpired_bounded_visual(self):
        m=self.message(11,hours_ago=1);m.file=SimpleNamespace(mime_type='video/webm',size=3);m.media=object()
        self.client.get_messages=AsyncMock(return_value=m)
        async def download(*args,**kwargs):yield b'abc'
        self.client.iter_download=download
        result=await self.collector._inspect_media(self.entity,self.source,11)
        self.assertEqual('ready',result['state'])
        self.assertEqual(b'abc',base64.b64decode(result['data_base64']))
        self.assertEqual({},self.backend.events)
        m.date=datetime.now(timezone.utc)-timedelta(hours=73)
        self.assertEqual('expired',(await self.collector._inspect_media(self.entity,self.source,11))['state'])
        m.date=datetime.now(timezone.utc);m.file.mime_type='audio/ogg'
        self.assertEqual('unsupported_for_visual_audit',(await self.collector._inspect_media(self.entity,self.source,11))['state'])

    async def test_untrusted_or_oversized_media_never_downloads(self):
        self.assertEqual('invalid_message',(await self.collector._inspect_media(self.entity,self.source,'11'))['state'])
        m=self.message(11,sender=22,post=False);m.file=SimpleNamespace(mime_type='video/mp4',size=1)
        self.client.get_messages=AsyncMock(return_value=m)
        self.assertEqual('untrusted',(await self.collector._inspect_media(self.entity,self.source,11))['state'])
        m.sender_id=None;m.post=True;m.file.size=9*1024*1024
        self.assertEqual('unsupported_for_visual_audit',(await self.collector._inspect_media(self.entity,self.source,11))['state'])
