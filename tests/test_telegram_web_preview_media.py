from datetime import datetime,timezone
from io import BytesIO
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PIL import Image
from telethon.tl.types import MessageMediaWebPage,Photo,PhotoSize,WebPageEmpty
from collector.telegram import TelegramCollector
from test_telegram_collector import Backend,Client,CONFIG,Message

class WebPreviewMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_downloads_only_the_attached_verified_photo_from_web_preview(self):
        client=Client();collector=TelegramCollector(CONFIG,backend=Backend(),client_factory=lambda *_:client)
        collector.client=client
        photo=Photo(id=1,access_hash=2,file_reference=b'proof',date=datetime.now(timezone.utc),
                    sizes=[PhotoSize(type='x',w=8,h=8,size=100)],dc_id=1)
        message=Message(1,media=True,mime='image/png')
        message.media=MessageMediaWebPage(webpage=WebPageEmpty(id=9))
        message.photo=photo
        body=BytesIO();Image.new('RGB',(8,8),'blue').save(body,format='PNG')
        seen=[]
        async def download(location,request_size):
            seen.append(location);yield body.getvalue()
        client.iter_download=download
        env={'S3_BUCKET':'isolated','S3_ENDPOINT_URL':'https://invalid.example',
             'S3_ACCESS_KEY':'test','S3_SECRET_KEY':'test'}
        try:
            with patch.dict(os.environ,env):
                result=await collector._prepare_media_candidate('-1001',message)
            self.assertEqual([photo],seen)
            self.assertEqual('image/png',result['mime_type'])
            self.assertEqual(len(body.getvalue()),result['record']['bytes'])
            message.photo=None;seen.clear()
            with patch.dict(os.environ,env),self.assertRaisesRegex(ValueError,'web_preview_photo_unavailable'):
                await collector._prepare_media_candidate('-1001',message)
            self.assertEqual([],seen)
        finally:await collector.disconnect()
