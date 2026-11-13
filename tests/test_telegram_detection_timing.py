"""Detection timing is observation metadata, not a new message version."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import os
import unittest
from unittest.mock import Mock, patch

from collector.telegram import TelegramCollector
import test_telegram_collector as collector_support
import test_telegram_api as api_support


class CollectorDetectionTests(unittest.IsolatedAsyncioTestCase):
    def make_collector(self):
        backend = collector_support.Backend()
        collector = TelegramCollector(collector_support.CONFIG, background_read_interval=0, backend=backend)
        collector.sources = {item['chat_id']: item for item in backend.source_items}
        collector._schedule_live_media = Mock()
        return collector, backend

    async def test_observation_is_not_part_of_version_identity(self):
        message = collector_support.Message(11)
        now = datetime.now(timezone.utc)
        payloads = []
        for stamp in (now, now + timedelta(seconds=5)):
            collector, backend = self.make_collector()
            await collector._stage_group('-1001', collector.sources['-1001'], [message],
                                         detected_at=stamp.isoformat())
            payloads.append(backend.calls[-1])
        self.assertEqual(payloads[0]['version_key'], payloads[1]['version_key'])
        self.assertNotEqual(payloads[0]['detected_at'], payloads[1]['detected_at'])

    async def test_failed_live_delivery_then_polling_does_not_restart_the_budget(self):
        collector, backend = self.make_collector()
        message = collector_support.Message(11)
        first = (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat()
        backend.fail_chat = '-1001'
        with self.assertRaises(ConnectionError):
            await collector._stage_group('-1001', collector.sources['-1001'], [message], detected_at=first)
        backend.fail_chat = None
        await collector._stage_group('-1001', collector.sources['-1001'], [message])
        self.assertEqual(first, backend.calls[-1]['detected_at'])

    async def test_new_edit_gets_its_own_observation_but_same_version_reuses_the_first(self):
        collector, backend = self.make_collector()
        message = collector_support.Message(11)
        first = (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat()
        await collector._stage_group('-1001', collector.sources['-1001'], [message], detected_at=first)
        old_key = backend.calls[-1]['version_key']
        message.message = 'Synthetic new explicit full-time under 2.5'
        message.edit_date = datetime.now(timezone.utc)
        second = datetime.now(timezone.utc).isoformat()
        await collector._stage_group('-1001', collector.sources['-1001'], [message], detected_at=second)
        self.assertNotEqual(old_key, backend.calls[-1]['version_key'])
        self.assertEqual(second, backend.calls[-1]['detected_at'])

    async def test_album_budget_starts_before_telethon_aggregation(self):
        collector, backend = self.make_collector()
        one = collector_support.Message(11, grouped=90, media=True)
        two = collector_support.Message(12, text='', grouped=90, media=True)
        first = datetime.now(timezone.utc) - timedelta(seconds=2)
        # Telethon invokes the individual event before its aggregated Album.
        class NewMessageEvent:
            message = one
            chat_id = -1001
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return first if tz is not None else first.replace(tzinfo=None)
        with patch('collector.telegram.events.NewMessage.Event', NewMessageEvent), \
             patch('collector.telegram.datetime', Clock):
            await collector._on_message(NewMessageEvent())
        self.assertEqual([], backend.calls)
        await collector._on_album(SimpleNamespace(chat_id=-1001, messages=[one, two]))
        self.assertEqual(first.isoformat(), backend.calls[-1]['detected_at'])

    async def test_observation_cache_is_bounded_and_keeps_no_raw_message(self):
        collector, _ = self.make_collector()
        for index in range(8200):
            collector._remember_detection('-1001', [collector_support.Message(index)])
        self.assertEqual(8192, len(collector.detection_times))
        self.assertTrue(all(len(key[1]) == 64 for key in collector.detection_times))


@unittest.skipUnless(os.environ.get('TELEGRAM_TEST_DATABASE_URL'), 'requires isolated Telegram DB')
class ApiDetectionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = api_support.TelegramApiTests.asyncSetUp
    asyncTearDown = api_support.TelegramApiTests.asyncTearDown
    event = api_support.TelegramApiTests.event
    post = api_support.TelegramApiTests.post

    async def test_pending_failed_ready_and_duplicate_keep_first_detection(self):
        first = self.event_time - timedelta(seconds=10)
        body = self.event(media=True) | {'detected_at': first.isoformat()}
        for changes in ({}, {'media_state': 'failed', 'last_error': 'TimeoutError'},
                        {'media_state': 'ready', 'last_error': None, 'media_objects': [{
                            'object_key': 'telegram/test/11', 'mime_type': 'image/jpeg',
                            'sha256': 'a' * 64, 'bytes': 100}]}):
            body.update(changes)
            response = await self.post(body)
            self.assertEqual(200, response.status_code, response.text)
        response = await self.post(body | {'detected_at': self.event_time.isoformat()})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(first, await self.pool.fetchval('SELECT detected_at FROM v2_expert_messages'))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_messages'))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_jobs'))

    async def test_new_edit_has_new_deadline_without_changing_prior_evidence(self):
        first = self.event_time - timedelta(seconds=15)
        self.assertEqual(200, (await self.post(self.event() | {'detected_at': first.isoformat()})).status_code)
        body = self.event(version='v2', text='Synthetic explicit edit', kind='edit', minutes=1)
        second = self.event_time - timedelta(seconds=1)
        response = await self.post(body | {'detected_at': second.isoformat()})
        self.assertEqual(200, response.status_code, response.text)
        stamps = await self.pool.fetch('SELECT detected_at FROM v2_expert_messages ORDER BY revision')
        self.assertEqual([first, second], [row['detected_at'] for row in stamps])

    async def test_future_detection_is_rejected_and_legacy_is_explicitly_unknown(self):
        response = await self.post(self.event() | {
            'detected_at': (self.event_time + timedelta(minutes=1)).isoformat()})
        self.assertEqual(422, response.status_code)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_messages'))
        response = await self.post(self.event())
        self.assertEqual(200, response.status_code, response.text)
        self.assertIsNone(await self.pool.fetchval('SELECT detected_at FROM v2_expert_messages'))
