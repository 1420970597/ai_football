"""Offline media scheduling contracts, independent of collector's main suite."""
import asyncio
from datetime import datetime, timedelta, timezone
from io import BytesIO
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from collector.telegram import TelegramCollector
from test_telegram_collector import Backend, Client, CONFIG, Message


def stored(identifier):
    return {'object_key': f'telegram/messages/test/{identifier}', 'mime_type': 'image/jpeg',
            'sha256': 'a' * 64, 'bytes': 100}


class VersionBackend(Backend):
    """Model API lease, immutable versions and tombstone enforcement."""
    def __init__(self):
        super().__init__()
        self.lease_valid = True
        self.tombstones = set()

    async def checkpoint(self, chat_id, owner):
        if not self.lease_valid:
            raise PermissionError('lease no longer owned')
        return await super().checkpoint(chat_id, owner)

    async def advance(self, chat_id, owner, last_message_id, previous):
        if not self.lease_valid:
            raise PermissionError('lease no longer owned')
        return await super().advance(chat_id, owner, last_message_id, previous)

    async def event(self, body):
        if not self.lease_valid:
            raise PermissionError('lease no longer owned')
        if body['event_kind'] == 'delete':
            self.tombstones.update(body['member_message_ids'])
            return {'persisted': True, 'state': 'deleted'}
        if self.tombstones.intersection(body['member_message_ids']):
            return {'persisted': True, 'state': 'deleted'}
        current = [value for value in self.events.values()
                   if value['platform_message_id'] == body['platform_message_id']]
        if current and max(value['source_version_at'] for value in current) > body['source_version_at']:
            return {'persisted': True, 'state': 'superseded'}
        key = (body['chat_id'], body['platform_message_id'], body['version_key'])
        previous = self.events.get(key)
        if previous and body['media_state'] == 'pending' and not body['media_objects']:
            body = body | {'media_objects': previous['media_objects']}
        result = await super().event(body)
        if result['state'] in {'pending', 'failed'}:
            result['media_objects'] = self.events[key]['media_objects']
        return result


class MediaSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = VersionBackend()
        self.client = Client()
        self.collector = TelegramCollector(CONFIG, backend=self.backend,
                                           client_factory=lambda *_: self.client)
        self.collector.client = self.client
        self.source = self.backend.source_items[0]
        self.collector.sources = {'-1001': self.source}

    async def asyncTearDown(self):
        await self.collector.disconnect()

    async def stage(self, *messages):
        self.client.messages['-1001'] = list(messages)
        return await self.collector._sync_source('-1001', self.source)

    async def test_history_stages_full_album_before_advancing_without_upload(self):
        calls = []
        async def upload(chat, message):
            calls.append(message.id)
            return stored(message.id)
        self.collector.media_store = upload
        result = await self.stage(Message(10, media=True, grouped=5), Message(11, media=True, grouped=5))
        self.assertEqual(11, result['last_message_id'])
        self.assertEqual([], calls)
        self.assertEqual(1, len(self.backend.events))
        event = next(iter(self.backend.events.values()))
        self.assertEqual('pending', event['media_state'])
        self.assertEqual([10, 11], event['member_message_ids'])
        self.assertEqual(2, len(event['media_manifest']))
        await self.collector.retry_pending()
        self.assertEqual([10, 11], calls)
        self.assertEqual('ready', next(iter(self.backend.events.values()))['media_state'])
        self.assertEqual(2, len(next(iter(self.backend.events.values()))['media_objects']))
        await self.collector.retry_pending()
        self.assertEqual([10, 11], calls)

    async def test_blocked_media_transfer_does_not_hold_live_group_lock(self):
        await self.stage(Message(1, media=True))
        entered, release = asyncio.Event(), asyncio.Event()
        async def upload(chat, message):
            entered.set()
            await release.wait()
            return stored(message.id)
        self.collector.media_store = upload
        retry = asyncio.create_task(self.collector.retry_pending())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(self.collector._live_messages('-1001', [Message(99)]), .5)
            self.assertEqual(1, self.backend.positions['-1001'])
            self.assertTrue(any(v['platform_message_id'] == '99' for v in self.backend.events.values()))
            self.assertFalse(retry.done())
        finally:
            release.set()
            await asyncio.wait_for(retry, 1)

    async def test_live_transfer_and_periodic_retry_share_version_deduplication(self):
        message = Message(1, media=True)
        await self.stage(message)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def upload(chat, item):
            calls.append(item.id)
            entered.set()
            await release.wait()
            return stored(item.id)

        self.collector.media_store = upload
        await asyncio.wait_for(self.collector._live_messages('-1001', [message]), .5)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(self.collector.retry_pending(), .5)
        self.assertEqual([1], calls)
        self.assertEqual('pending', next(iter(self.backend.events.values()))['media_state'])
        release.set()
        await asyncio.gather(*tuple(self.collector.live_media_tasks.values()))
        self.assertEqual('ready', next(iter(self.backend.events.values()))['media_state'])

    async def test_reconciliation_persists_pending_edit_without_downloading(self):
        first = Message(1, text='old')
        await self.stage(first)
        self.backend.observed_items = [{'content': next(iter(self.backend.events.values()))}]
        first.message, first.media = 'edited image', SimpleNamespace(id=1)
        first.file = SimpleNamespace(mime_type='image/jpeg', size=100, width=800, height=600)
        first.edit_date = first.date + timedelta(minutes=1)
        async def upload(*_):
            raise AssertionError('reconciliation cannot transfer media')
        self.collector.media_store = upload
        await asyncio.wait_for(self.collector._reconcile_known('-1001', self.source), 1)
        self.assertEqual(2, len(self.backend.events))
        self.assertEqual('pending', list(self.backend.events.values())[-1]['media_state'])

    async def test_media_timeout_is_durable_failed_then_one_retry_recovers(self):
        await self.stage(Message(1, media=True))
        calls = []
        async def upload(chat, message):
            calls.append(message.id)
            await asyncio.Future()
        self.collector.media_store = upload
        with patch('collector.telegram.MEDIA_TRANSFER_SECONDS', .02):
            await asyncio.wait_for(self.collector.retry_pending(), .5)
        event = next(iter(self.backend.events.values()))
        self.assertEqual('failed', event['media_state'])
        self.assertEqual('TimeoutError', event['last_error'])
        self.assertEqual([1], calls)
        async def recover(chat, message):
            calls.append(message.id)
            return stored(message.id)
        self.collector.media_store = recover
        await self.collector.retry_pending()
        self.assertEqual([1, 1], calls)
        self.assertEqual('ready', next(iter(self.backend.events.values()))['media_state'])

    async def test_batch_deadline_cancels_without_marking_media_ready(self):
        await self.stage(Message(1, media=True))
        async def upload(*_):
            await asyncio.Future()
        self.collector.media_store = upload
        with patch('collector.telegram.MEDIA_RETRY_BATCH_SECONDS', .02):
            await asyncio.wait_for(self.collector.retry_pending(), .5)
        self.assertEqual('pending', next(iter(self.backend.events.values()))['media_state'])
        self.assertFalse(self.collector.locks['-1001'].locked())

    async def test_pending_media_retries_run_with_bounded_concurrency(self):
        await self.stage(Message(1, media=True), Message(2, media=True), Message(3, media=True))
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def upload(chat, message):
            calls.append(message.id)
            if len(calls) == 2:
                entered.set()
            await release.wait()
            return stored(message.id)

        self.collector.media_store = upload
        retry = asyncio.create_task(self.collector.retry_pending())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual([1, 2], calls)
            self.assertFalse(retry.done())
        finally:
            release.set()
            await asyncio.wait_for(retry, 1)
        self.assertEqual([1, 2], calls)
        self.assertEqual(['ready', 'ready', 'pending'],
                         [value['media_state'] for value in self.backend.events.values()])
        await self.collector.retry_pending()
        self.assertEqual([1, 2, 3], calls)
        self.assertEqual({'ready'}, {value['media_state'] for value in self.backend.events.values()})

    async def test_lease_loss_after_transfer_cannot_commit_ready(self):
        await self.stage(Message(1, media=True))
        async def upload(chat, message):
            self.backend.lease_valid = False
            return stored(message.id)
        self.collector.media_store = upload
        await self.collector.retry_pending()
        self.assertEqual('pending', next(iter(self.backend.events.values()))['media_state'])
        self.assertEqual(1, self.backend.positions['-1001'])
        self.assertFalse(any(v['media_state'] == 'ready' for v in self.backend.calls))

    async def test_failed_initial_pending_persistence_never_advances_history(self):
        self.backend.fail_chat = '-1001'
        self.client.messages['-1001'] = [Message(1, media=True)]
        with self.assertRaises(ConnectionError):
            await self.collector._sync_source('-1001', self.source)
        self.assertEqual(0, self.backend.positions.get('-1001', 0))

    async def test_expired_lease_during_pending_persistence_does_not_advance(self):
        self.client.messages['-1001'] = [Message(1, media=True)]
        original = self.backend.event
        async def lose_lease(body):
            result = await original(body)
            self.backend.lease_valid = False
            return result
        self.backend.event = lose_lease
        with self.assertRaises(PermissionError):
            await self.collector._sync_source('-1001', self.source)
        self.assertEqual(0, self.backend.positions.get('-1001', 0))
        self.assertEqual('pending', next(iter(self.backend.events.values()))['media_state'])

    async def test_new_live_edit_supersedes_old_inflight_media_version(self):
        old = Message(1, text='old', media=True)
        await self.stage(old)
        entered, release = asyncio.Event(), asyncio.Event()
        async def upload(chat, message):
            entered.set(); await release.wait()
            return stored(message.id)
        self.collector.media_store = upload
        retry = asyncio.create_task(self.collector.retry_pending())
        live_tasks = ()
        try:
            await asyncio.wait_for(entered.wait(), 1)
            edited = Message(1, text='new', media=True, edited=old.date + timedelta(minutes=1))
            await asyncio.wait_for(self.collector._live_messages('-1001', [edited]), .5)
            live_tasks = tuple(self.collector.live_media_tasks.values())
        finally:
            release.set()
            await asyncio.wait_for(retry, 1)
            if live_tasks:
                await asyncio.wait_for(asyncio.gather(*live_tasks), 1)
        self.assertEqual(2, len(self.backend.events))
        self.assertEqual({'pending', 'ready'}, {v['media_state'] for v in self.backend.events.values()})
        self.assertEqual({'old': 'pending', 'new': 'ready'}, {
            value['raw_text']: value['media_state'] for value in self.backend.events.values()
        })

    async def test_partial_album_failure_keeps_remote_object_reference(self):
        await self.stage(Message(10, media=True, grouped=5), Message(11, media=True, grouped=5))
        async def upload(chat, message):
            if message.id == 11:
                raise ConnectionError('second image failed')
            return stored(message.id)
        self.collector.media_store = upload
        await self.collector.retry_pending()
        event = next(iter(self.backend.events.values()))
        self.assertEqual('failed', event['media_state'])
        self.assertEqual([stored(10)], event['media_objects'])
        self.assertEqual([10, 11], event['member_message_ids'])
        async def failed_before_first_object(*_):
            raise ConnectionError('later retry still unavailable')
        self.collector.media_store = failed_before_first_object
        await self.collector.retry_pending()
        self.assertEqual([stored(10)], next(iter(self.backend.events.values()))['media_objects'])

    async def test_live_deletion_during_transfer_cannot_resurrect_media(self):
        await self.stage(Message(1, media=True))
        entered, release = asyncio.Event(), asyncio.Event()
        async def upload(chat, message):
            entered.set(); await release.wait()
            return stored(message.id)
        self.collector.media_store = upload
        retry = asyncio.create_task(self.collector.retry_pending())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await self.collector._on_delete(SimpleNamespace(chat_id=-1001, deleted_ids=[1]))
        finally:
            release.set(); await asyncio.wait_for(retry, 1)
        self.assertEqual({1}, self.backend.tombstones)
        self.assertEqual('pending', next(iter(self.backend.events.values()))['media_state'])

    async def test_registered_candidate_retry_avoids_duplicate_commit(self):
        message = Message(1, media=True)
        await self.stage(message)
        _, payload = await self.collector._stage_group('-1001', self.source, [message])
        record = stored(1)
        payload['media_objects'] = [record]
        committed = []
        original_commit = self.collector._commit_media_payload
        async def commit(chat, value):
            committed.append(value['media_state'])
            return await original_commit(chat, value)
        async def upload(chat, item, *, stage_object):
            await stage_object(record)
            return record
        self.collector._commit_media_payload = commit
        self.collector._store_media = upload
        await self.collector._transfer_pending_media('-1001', payload, [message])
        self.assertEqual(['ready'], committed)
        self.assertEqual('ready', next(iter(self.backend.events.values()))['media_state'])

    async def test_cached_checkpoint_still_requires_backend_lease_at_media_commit(self):
        message = Message(1, media=True)
        await self.stage(message)
        _, payload = await self.collector._stage_group('-1001', self.source, [message])
        self.backend.lease_valid = False
        with self.assertRaisesRegex(PermissionError, 'lease no longer owned'):
            await self.collector._commit_media_payload('-1001', payload | {'media_state':'ready','media_objects':[stored(1)]})
        self.assertEqual('pending', next(iter(self.backend.events.values()))['media_state'])

    async def test_media_commit_uses_atomic_event_lease_without_second_claim(self):
        message = Message(1, media=True)
        await self.stage(message)
        _, payload = await self.collector._stage_group('-1001', self.source, [message])
        async def redundant_claim(*_):
            raise AssertionError('media commit must rely on the event lease check')
        self.collector._checkpoint = redundant_claim
        self.collector.checkpoint_verified_at.clear()
        result = await self.collector._commit_media_payload('-1001',payload | {'media_state':'ready','media_objects':[stored(1)]})
        self.assertEqual('ready', result['state'])

    async def test_s3_candidate_registered_before_thread_and_late_timeout_not_ready(self):
        message = Message(1, media=True, mime='image/png')
        await self.stage(message)
        body = BytesIO(); Image.new('RGB', (8, 8), 'blue').save(body, format='PNG')
        self.client.download_chunks = [body.getvalue()]
        entered, release = threading.Event(), threading.Event()
        class S3:
            def put_object(inner, **kwargs):
                entered.set(); release.wait(1.0)
            def close(inner):
                pass
        env = {'S3_BUCKET':'test-private','S3_ENDPOINT_URL':'https://invalid.example',
               'S3_ACCESS_KEY':'test-only','S3_SECRET_KEY':'test-only'}
        with patch.dict(os.environ, env), patch('collector.telegram.boto3.client', return_value=S3()), patch('collector.telegram.MEDIA_TRANSFER_SECONDS', .2):
            try:
                await asyncio.wait_for(self.collector.retry_pending(), .5)
                self.assertTrue(entered.is_set())
                event = next(iter(self.backend.events.values()))
                self.assertEqual('failed', event['media_state'])
                self.assertEqual(1, len(event['media_objects']))
                self.assertTrue(event['media_objects'][0]['object_key'].startswith('v2/telegram/messages/-1001/1/'))
            finally:
                release.set()
        await asyncio.sleep(.02)
        self.assertEqual('failed', next(iter(self.backend.events.values()))['media_state'])


if __name__ == '__main__':
    unittest.main()
