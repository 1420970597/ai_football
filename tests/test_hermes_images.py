import asyncio
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
    async def test_reply_images_are_native_separated_context_with_shared_limits(self):
        current = {'object_key': 'current.png', 'mime_type': 'image/png'}
        reply = {'object_key': 'reply.png', 'mime_type': 'image/png',
                 'sha256': hashlib.sha256(PNG).hexdigest(), 'bytes': len(PNG)}
        message = self.message([current])
        message['reply_archive'] = {'id': 'archived-reply', 'media_state': 'ready', 'media_objects': [reply]}
        with patch.object(blobstore, 'read_image', AsyncMock(return_value=PNG)) as read:
            parts = await hermes_client._content_parts(message, 'review', 'attempt')
        self.assertEqual(['current.png', 'reply.png'], [call.args[0] for call in read.await_args_list])
        self.assertEqual(['text', 'text', 'image_url', 'text', 'image_url'], [part['type'] for part in parts])
        self.assertIn('被引用原文', parts[3]['text'])
        self.assertIn('不能据此猜新盘口', parts[0]['text'])
        message['media_objects'] = [current]*10
        with patch.object(blobstore, 'read_image', AsyncMock(return_value=PNG)) as read:
            with self.assertRaisesRegex(RuntimeError, 'image count'):
                await hermes_client._content_parts(message, 'review', 'attempt')
            read.assert_not_awaited()
        message['media_objects'] = [current]
        with patch.object(blobstore, 'read_image', AsyncMock(return_value=PNG)), \
             patch.object(hermes_client, 'MAX_REVIEW_IMAGE_BYTES', len(PNG)):
            with self.assertRaisesRegex(RuntimeError, 'total bytes'):
                await hermes_client._content_parts(message, 'review', 'attempt')

    async def test_incomplete_reply_is_retryable_and_deleted_or_unsupported_images_are_not_read(self):
        from app.worker import failure_category
        for state in ('pending', 'failed'):
            message = self.message([])
            message['reply_archive'] = {'media_state': state, 'media_objects': []}
            with patch.object(blobstore, 'read_image', AsyncMock()) as read:
                with self.assertRaises(hermes_client.ReplyMediaPending) as raised:
                    await hermes_client._content_parts(message, 'review', 'attempt')
                self.assertEqual(('reply_media_pending', True), failure_category(raised.exception))
                read.assert_not_awaited()
        for reply in ({'media_state': 'ready', 'deleted': True}, {'media_state': 'unsupported'}):
            message = self.message([])
            message['reply_archive'] = reply | {'media_objects': [{'object_key': 'must-not-read.png'}]}
            with patch.object(blobstore, 'read_image', AsyncMock()) as read:
                parts = await hermes_client._content_parts(message, 'review', 'attempt')
                self.assertEqual(['text'], [part['type'] for part in parts])
                read.assert_not_awaited()

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

    async def test_retry_guidance_keeps_pair_identity_and_never_invents_alias(self):
        parts=await hermes_client._content_parts(self.message([]),'review','attempt')
        text=parts[0]['text']
        self.assertIn('仍同时传原文team_a/team_b',text)
        self.assertIn('保留身份未核验',text)
        self.assertIn('不能把候选全名',text)

    async def test_album_images_prepare_concurrently_with_a_bounded_reader_pool(self):
        active = peak = 0

        async def read_image(_key):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(.01)
                return PNG
            finally:
                active -= 1

        media = [{'object_key':f'v2/tg/image{i}.png','mime_type':'image/png'} for i in range(4)]
        with patch.object(blobstore, 'read_image', side_effect=read_image):
            parts = await hermes_client._content_parts(self.message(media), 'review', 'attempt')
        self.assertEqual(2, peak)
        self.assertEqual(5, len(parts))
        self.assertEqual(['image_url'] * 4, [part['type'] for part in parts[1:]])

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
