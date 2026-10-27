import base64
from datetime import datetime, timezone
import hashlib
import unittest
from unittest.mock import AsyncMock, patch

from app import blobstore, hermes_client
from io import BytesIO
from PIL import Image


def png_bytes():
    output = BytesIO()
    Image.new('RGB', (48, 32), 'red').save(output, format='PNG')
    return output.getvalue()


PNG = png_bytes()


class HermesImagesTests(unittest.IsolatedAsyncioTestCase):
    def test_decoded_image_pixels_and_corrupt_payload_are_rejected(self):
        self.assertEqual(48, blobstore.validate_image(PNG)['width'])
        with patch.object(blobstore, 'MAX_IMAGE_PIXELS', 100):
            with self.assertRaisesRegex(RuntimeError, 'pixel limit'):
                blobstore.validate_image(PNG)
        with self.assertRaisesRegex(RuntimeError, 'decoding failed'):
            blobstore.validate_image(b'\x89PNG\r\n\x1a\nrest')

    def message(self, media):
        return {'source_name':'测试群', 'display_nickname':'专家昵称', 'raw_text':'Alpha 对 Beta，大2.5；另买主队让0.25',
                'posted_at':datetime.now(timezone.utc), 'reply_text':'赛前推荐', 'media_objects':media}

    async def test_all_images_are_structured_native_parts_with_context(self):
        media = [{'object_key':f'v2/tg/image{i}.png','mime_type':'image/png'} for i in range(2)]
        with patch.object(blobstore, 'read_image', AsyncMock(return_value=PNG)) as read:
            parts = await hermes_client._content_parts(self.message(media), 'review', 'attempt')
        self.assertEqual(2, read.await_count)
        self.assertEqual(['text','image_url','image_url'], [p['type'] for p in parts])
        self.assertIn('赛前推荐', parts[0]['text'])
        self.assertIn('专家昵称', parts[0]['text'])
        self.assertEqual(PNG, base64.b64decode(parts[1]['image_url']['url'].split(',',1)[1]))
        self.assertEqual('high', parts[1]['image_url']['detail'])

    async def test_missing_image_fails_whole_review_instead_of_dropping_image(self):
        media = [{'object_key':'v2/missing.png','mime_type':'image/png'}]
        with patch.object(blobstore, 'read_image', AsyncMock(side_effect=RuntimeError('object unavailable'))):
            with self.assertRaises(RuntimeError):
                await hermes_client._content_parts(self.message(media), 'review', 'attempt')

    async def test_historical_mode_and_original_posted_time_are_explicit_model_context(self):
        message = self.message([]) | {'review_mode': 'historical_replay',
                                     'posted_at': datetime(2021, 2, 3, tzinfo=timezone.utc)}
        parts = await hermes_client._content_parts(message, 'review', 'attempt')
        self.assertIn('historical_replay', parts[0]['text'])
        self.assertIn('2021-02-03T00:00:00+00:00', parts[0]['text'])
        self.assertIn('禁止把旧推荐绑定当前比赛报价', parts[0]['text'])

    async def test_image_failure_remains_fatal_for_historical_mode(self):
        media = [{'object_key':'v2/missing.png','mime_type':'image/png'}]
        with patch.object(blobstore, 'read_image', AsyncMock(side_effect=RuntimeError('object unavailable'))):
            with self.assertRaises(RuntimeError):
                await hermes_client._content_parts(self.message(media) | {'review_mode': 'historical_replay'}, 'review', 'attempt')

    async def test_mime_conflict_html_and_total_budget_are_rejected(self):
        media = [{'object_key':'v2/image','mime_type':'image/jpeg'}]
        with patch.object(blobstore, 'read_image', AsyncMock(return_value=PNG)):
            with self.assertRaisesRegex(RuntimeError, 'conflicts'):
                await hermes_client._content_parts(self.message(media), 'review', 'attempt')
        with self.assertRaises(RuntimeError):
            blobstore.image_mime(b'<html>Login expired</html>')
        with patch.object(blobstore, 'read_image', AsyncMock(return_value=PNG)), patch.object(hermes_client, 'MAX_REVIEW_IMAGE_BYTES', len(PNG)):
            with self.assertRaisesRegex(RuntimeError, 'total bytes'):
                await hermes_client._content_parts(self.message([{'object_key':str(i),'mime_type':'image/png'} for i in range(2)]), 'r', 'a')

    async def test_remote_media_digest_and_byte_evidence_are_verified_before_model_input(self):
        digest = hashlib.sha256(PNG).hexdigest()
        media = [{'object_key': 'v2/image.png', 'mime_type': 'image/png', 'sha256': digest, 'bytes': len(PNG)}]
        with patch.object(blobstore, 'read_image', AsyncMock(return_value=PNG)):
            parts = await hermes_client._content_parts(self.message(media), 'r', 'a')
        self.assertEqual('image_url', parts[1]['type'])

        with patch.object(blobstore, 'read_image', AsyncMock(return_value=PNG)):
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                await hermes_client._content_parts(self.message([{**media[0], 'sha256': '0' * 64}]), 'r', 'a')
        with patch.object(blobstore, 'read_image', AsyncMock(return_value=PNG)):
            with self.assertRaisesRegex(RuntimeError, 'byte count mismatch'):
                await hermes_client._content_parts(self.message([{**media[0], 'bytes': len(PNG) + 1}]), 'r', 'a')
