import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from io import BytesIO
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image
from telethon import utils
from telethon.errors import (AuthKeyDuplicatedError, FloodWaitError,
                             PasswordHashInvalidError, SessionPasswordNeededError)
from telethon.tl.types import Channel, Chat, ChatPhotoEmpty, ChatParticipantAdmin, ChatParticipants
from telethon.tl.functions.messages import GetFullChatRequest

from collector.telegram import TelegramCollector, MAX_MEDIA_BYTES, _valid_image, _image_dimensions


CONFIG = {"telegram_api_id": "12345", "telegram_api_hash": "test-api-hash-only",
          "telegram_session": "test-session-only", "telegram_source_enabled": "true",
          "telegram_account": json.dumps({"id": "1234", "display_name": "测试账号"})}


class Message:
    def __init__(self, identifier, text="阿森纳 全场大2.5", *, media=False, grouped=None, sender=7, edited=None, mime="image/jpeg"):
        self.id, self.message = identifier, text
        self.sender_id, self.grouped_id, self.edit_date = sender, grouped, edited
        self.date = datetime(2026, 10, 26, tzinfo=timezone.utc)
        self.reply_to_msg_id = None
        self.media = SimpleNamespace(id=identifier) if media else None
        self.file = SimpleNamespace(mime_type=mime, size=100, width=800, height=600) if media else None

    async def get_sender(self):
        return SimpleNamespace(id=self.sender_id, first_name="专家", last_name="甲", username="test-expert")


class Backend:
    def __init__(self):
        self.positions = {}
        self.events = {}
        self.calls = []
        self.checkpoints = []
        self.backfill_updates = []
        self.source_items = [{"chat_id": "-1001", "trusted_sender_ids": ["7"], "allow_media": True}]
        self.fail_chat = None
        self.save_fails = False
        self.saved_sessions = []
        self.observed_items = []
        self.orphan_items = []
        self.orphan_acks = []

    async def sources(self):
        return self.source_items

    async def checkpoint(self, chat_id, owner):
        self.checkpoints.append((chat_id, owner))
        return {"last_message_id": self.positions.get(chat_id, 0)}

    async def advance(self, chat_id, owner, last_message_id, previous):
        if self.positions.get(chat_id, 0) != previous:
            raise RuntimeError("checkpoint mismatch")
        self.positions[chat_id] = last_message_id

    async def advance_author_backfill(self, chat_id, owner, sender_id, cursor, previous, complete):
        self.backfill_updates.append((chat_id, sender_id, previous, cursor, complete))
        task = next(item for item in self.source_items[0]['author_backfills'] if item['sender_id'] == sender_id)
        if task['cursor'] != previous:
            raise RuntimeError('author backfill cursor mismatch')
        task['cursor'] = cursor
        if complete:
            self.source_items[0]['author_backfills'].remove(task)

    async def event(self, body):
        if body["chat_id"] == self.fail_chat:
            raise ConnectionError("test backend unavailable")
        self.calls.append(copy.deepcopy(body))
        key = (body["chat_id"], body["platform_message_id"], body["version_key"])
        existing = self.events.get(key)
        if existing and existing["media_state"] == "ready":
            return {"persisted": True, "state": "ready"}
        self.events[key] = copy.deepcopy(body)
        return {"persisted": True, "state": body["media_state"]}

    async def pending(self):
        return [{"chat_id": value["chat_id"], "content": value} for value in self.events.values() if value["media_state"] in {"pending", "failed"}]

    async def save_session(self, session, account):
        if self.save_fails:
            raise ConnectionError("test persistence unavailable")
        self.saved_sessions.append((session, account))
        return {"persisted": True}

    async def observed(self, chat_id, owner, limit=50):
        return [self.claimed_observation(item) for item in self.observed_items[:limit]]

    def claimed_observation(self, item):
        return {**item, 'id': str(uuid.uuid4()), 'version_key': item['content'].get('version_key', '0'*64),
                'claim_id': str(uuid.uuid4()),
                'claim_lease_until': (datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat()}

    async def acknowledge_observed(self, chat_id, owner, reconciliation):
        return {'acknowledged': True, 'duplicate': False}

    async def media_orphans(self, owner, *, older_than_hours=24, limit=100):
        return self.orphan_items[:limit]

    async def acknowledge_media_orphans(self, owner, object_keys):
        self.orphan_acks.append((owner, list(object_keys)))
        return {"acknowledged": len(object_keys)}


class Client:
    def __init__(self, messages=None):
        self.messages = messages or {"-1001": []}
        self.connected, self.authorized = False, True
        self.authorization_error = None
        self.request_error = None
        self.connect_count, self.disconnect_count = 0, 0
        self.handlers = []
        self.query_limits = []
        self.session = object()
        self.qr = None
        self.account_id = 1234
        self.download_chunks = [b"\xff\xd8\xfftest-jpeg"]

    async def connect(self):
        self.connect_count += 1
        self.connected = True

    async def disconnect(self):
        self.disconnect_count += 1
        self.connected = False

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        if self.authorization_error:
            raise self.authorization_error
        return self.authorized

    def add_event_handler(self, fn, event):
        self.handlers.append((fn, event))

    async def get_entity(self, value):
        if self.request_error:
            raise self.request_error
        return Chat(id=abs(int(value)), title="测试群", photo=ChatPhotoEmpty(), participants_count=105,
                    date=datetime(2026, 10, 26, tzinfo=timezone.utc), version=1)

    async def __call__(self, request):
        if isinstance(request, GetFullChatRequest):
            users = [SimpleNamespace(id=index, first_name=f"专家{index}", last_name="", username=None) for index in range(1, 106)]
            admins = [ChatParticipantAdmin(user_id=user.id, inviter_id=1, date=datetime.now(timezone.utc)) for user in users]
            return SimpleNamespace(full_chat=SimpleNamespace(participants=ChatParticipants(request.chat_id, admins, 1)), users=users)
        raise AssertionError("unexpected fake RPC")

    async def iter_messages(self, entity, min_id=0, reverse=False, limit=None, **filters):
        self.query_limits.append(limit)
        messages = self.messages.get(str(utils.get_peer_id(entity)), [])
        if 'from_user' in filters:
            messages = [item for item in messages if item.sender_id == filters['from_user']]
        if 'max_id' in filters:
            messages = [item for item in messages if item.id < filters['max_id']]
        if min_id:
            messages = [item for item in messages if item.id > min_id]
        messages = sorted(messages, key=lambda item: item.id, reverse=not reverse)
        for message in messages[:limit] if limit is not None else messages:
            if message.id > min_id:
                yield message

    async def get_messages(self, entity, ids):
        return [item for item in self.messages.get(str(utils.get_peer_id(entity)), []) if item.id in ids]

    async def iter_download(self, media, request_size):
        for chunk in self.download_chunks:
            yield chunk

    async def qr_login(self):
        return self.qr

    async def get_me(self):
        return SimpleNamespace(id=self.account_id, first_name="测试", last_name="账号", username="test-account")

    async def sign_in(self, password):
        if password != "test-correct-password":
            raise PasswordHashInvalidError(None)
        self.authorized = True

    async def log_out(self):
        self.authorized = False

    async def iter_participants(self, entity, filter, limit):
        for index in range(1, 106):
            yield SimpleNamespace(id=index, first_name=f"专家{index}", last_name="", username=None)


class QR:
    def __init__(self, client, needs_password=False, immediate=False):
        self.url = "tg://login?token=test-only-qr"
        self.expires = datetime.now(timezone.utc) + timedelta(seconds=30)
        self.needs_password, self.immediate = needs_password, immediate
        self.client = client

    async def wait(self, timeout):
        if self.needs_password:
            raise SessionPasswordNeededError(None)
        if self.immediate:
            self.client.authorized = True
            return
        await asyncio.Future()

    async def recreate(self):
        self.expires = datetime.now(timezone.utc) + timedelta(seconds=30)


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = Backend()
        self.client = Client()
        self.created = []

        def factory(session, api_id, api_hash):
            self.created.append(session)
            return self.client

        self.collector = TelegramCollector(CONFIG, backend=self.backend, client_factory=factory)

    async def asyncTearDown(self):
        await self.collector.disconnect()

    async def test_more_than_twenty_messages_and_connection_reuse(self):
        self.client.messages["-1001"] = [Message(index) for index in range(1, 106)]
        with patch('collector.telegram.SYNC_HISTORY_SLICE_SECONDS', 3600):
            pages = [(await self.collector.sync_once())["sources"]["-1001"] for _ in range(5)]
        self.assertEqual(105, sum(page["observed"] for page in pages))
        self.assertEqual("caught_up", pages[-1]["state"])
        self.assertEqual(105, len(self.backend.events))
        self.assertEqual(105, self.backend.positions["-1001"])
        self.assertEqual([None] * 5, self.client.query_limits)
        await self.collector.sync_once()
        self.assertEqual(1, self.client.connect_count)
        self.assertEqual(1, len(self.created))

    async def test_trusted_author_backfill_is_durable_and_does_not_rewind_group_checkpoint(self):
        self.backend.positions['-1001'] = 4
        self.backend.source_items[0]['author_backfills'] = [{
            'sender_id': '7', 'cursor': 4, 'until_id': 3,
            'since': '2026-10-25T00:00:00+00:00',
        }]
        self.client.messages['-1001'] = [Message(index) for index in range(1, 4)]
        result = await self.collector.sync_once()
        self.assertEqual('caught_up', result['sources']['-1001']['state'])
        self.assertEqual('complete', result['sources']['-1001']['author_backfill']['state'])
        self.assertEqual(4, self.backend.positions['-1001'])
        self.assertEqual(3, len(self.backend.events))
        self.assertEqual([('-1001', '7', 4, 1, True)], self.backend.backfill_updates)

    async def test_author_backfill_does_not_advance_when_event_persistence_fails(self):
        self.backend.positions['-1001'] = 4
        self.backend.fail_chat = '-1001'
        self.backend.source_items[0]['author_backfills'] = [{
            'sender_id': '7', 'cursor': 4, 'until_id': 3,
            'since': '2026-10-25T00:00:00+00:00',
        }]
        self.client.messages['-1001'] = [Message(3)]
        result = await self.collector.sync_once()
        self.assertEqual('unavailable', result['sources']['-1001']['state'])
        self.assertEqual([], self.backend.backfill_updates)
        self.assertEqual(4, self.backend.source_items[0]['author_backfills'][0]['cursor'])

    async def test_more_than_one_thousand_history_messages_resume_without_truncation(self):
        self.client.messages['-1001'] = [Message(index) for index in range(1, 1006)]
        with patch('collector.telegram.SYNC_HISTORY_SLICE_SECONDS', 3600):
            pages = [(await self.collector.sync_once())['sources']['-1001'] for _ in range(41)]
        self.assertEqual(1005, sum(item['observed'] for item in pages))
        self.assertEqual('caught_up', pages[-1]['state'])
        self.assertEqual(1005, self.backend.positions['-1001'])
        self.assertEqual(1005, len(self.backend.events))
        self.assertEqual(1, self.client.connect_count)

    async def test_persistence_failure_does_not_advance_or_mark_seen(self):
        self.client.messages["-1001"] = [Message(1)]
        self.backend.fail_chat = "-1001"
        await self.collector.sync_once()
        self.assertEqual(0, self.backend.positions.get("-1001", 0))
        self.backend.fail_chat = None
        await self.collector.sync_once()
        self.assertEqual(1, self.backend.positions["-1001"])
        self.assertEqual(1, len(self.backend.events))

    async def test_history_slices_resume_persisted_high_water_without_losing_messages(self):
        self.client.messages['-1001'] = [Message(index) for index in range(1, 9)]
        with patch('collector.telegram.SYNC_HISTORY_MAX_MESSAGES', 3):
            states = [(await self.collector.sync_once())['sources']['-1001'] for _ in range(3)]
        self.assertEqual(['catching_up', 'catching_up', 'caught_up'], [item['state'] for item in states])
        self.assertEqual([3, 6, 8], [item['last_message_id'] for item in states])
        self.assertEqual(8, len(self.backend.events))
        self.assertEqual(8, self.backend.positions['-1001'])

    async def test_live_message_does_not_wait_for_history_persistence(self):
        self.collector.client = self.client
        self.collector.sources = {'-1001': self.backend.source_items[0]}
        self.client.messages['-1001'] = [Message(index) for index in range(1, 8)]
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.collector._process_message
        async def process(chat_id, source, message, entity=None):
            if message.id == 1:
                entered.set(); await release.wait()
            return await original(chat_id, source, message, entity)
        self.collector._process_message = process
        with patch('collector.telegram.SYNC_HISTORY_MAX_MESSAGES', 2):
            history = asyncio.create_task(self.collector._sync_source('-1001', self.backend.source_items[0]))
            await entered.wait()
            live = asyncio.create_task(self.collector._live_messages('-1001', [Message(99)]))
            await asyncio.sleep(0); release.set()
            self.assertEqual('catching_up', (await history)['state'])
            await asyncio.wait_for(live, 1)
        self.assertEqual({'1', '2', '99'}, {item['platform_message_id'] for item in self.backend.calls})
        self.assertEqual(2, self.backend.positions['-1001'])
        await self.collector._sync_source('-1001', self.backend.source_items[0])
        self.assertEqual(7, self.backend.positions['-1001'])
        self.assertEqual(8, len(self.backend.events))

    async def test_live_image_starts_media_transfer_after_staging_without_holding_chat_lock(self):
        self.collector.client = self.client
        self.collector.sources = {'-1001': self.backend.source_items[0]}
        entered, release, completed = asyncio.Event(), asyncio.Event(), []

        async def complete(chat_id, payload, members):
            self.assertFalse(self.collector.locks[chat_id].locked())
            self.assertEqual('pending', payload['media_state'])
            entered.set()
            await release.wait()
            ready = payload | {'media_state': 'ready', 'media_objects': [{
                'object_key': 'telegram/test/live-image', 'mime_type': 'image/jpeg',
                'sha256': 'a' * 64, 'bytes': 100,
            }]}
            completed.append((chat_id, len(members)))
            return await self.backend.event(ready)

        self.collector._complete_pending_media = complete
        await asyncio.wait_for(self.collector._live_messages('-1001', [Message(99, media=True)]), .5)
        await asyncio.wait_for(entered.wait(), .5)
        self.assertEqual(['pending'], [item['media_state'] for item in self.backend.calls])
        release.set()
        await asyncio.gather(*tuple(self.collector.live_media_tasks.values()))
        self.assertEqual([('-1001', 1)], completed)
        states = [item['media_state'] for item in self.backend.calls]
        self.assertEqual(['pending', 'ready'], states)

    async def test_live_single_message_uses_cached_author_without_entity_rpc(self):
        self.collector.sources = {'-1001': self.backend.source_items[0]}
        message = Message(100)
        message.sender = SimpleNamespace(id=7, first_name='专家', last_name='甲', username='cached-expert')
        message.get_sender = AsyncMock(side_effect=AssertionError('cached author should avoid Telegram RPC'))
        self.collector._entity = AsyncMock(side_effect=AssertionError('single message does not need group entity'))

        await self.collector._live_messages('-1001', [message], entity=object())

        message.get_sender.assert_not_awaited()
        self.collector._entity.assert_not_awaited()
        self.assertEqual('专家 甲', self.backend.calls[0]['sender_nickname'])

    async def test_trusted_broadcast_channel_post_without_sender_id_uses_channel_identity(self):
        self.collector.sources = {'-1001': {'chat_id': '-1001', 'display_name': '可信频道',
                                            'trusted_sender_ids': ['-1001'], 'allow_media': True}}
        message = Message(101, sender=None)
        message.post = True
        message.get_sender = AsyncMock(side_effect=AssertionError('channel post has no individual sender'))

        await self.collector._live_messages('-1001', [message])

        self.assertEqual(1, len(self.backend.calls))
        self.assertEqual('-1001', self.backend.calls[0]['sender_id'])
        self.assertEqual('可信频道', self.backend.calls[0]['sender_nickname'])

    async def test_missing_sender_id_in_regular_group_is_not_trusted_by_channel_fallback(self):
        self.collector.sources = {'-1001': {'chat_id': '-1001', 'trusted_sender_ids': ['-1001'], 'allow_media': True}}
        message = Message(102, sender=None)
        message.post = False

        await self.collector._live_messages('-1001', [message])

        self.assertEqual([], self.backend.calls)

    async def test_sender_and_reply_lookups_run_concurrently(self):
        self.collector.sources = {'-1001': self.backend.source_items[0]}
        sender_entered, reply_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        message = Message(105)
        message.reply_to_msg_id = 44

        async def get_sender():
            sender_entered.set()
            await release.wait()
            return SimpleNamespace(id=7, first_name='专家', last_name='甲', username=None)

        async def get_reply():
            reply_entered.set()
            await release.wait()
            return SimpleNamespace(id=44, sender_id=8, message='原始上下文', media=None)

        message.get_sender = get_sender
        message.get_reply_message = get_reply
        live = asyncio.create_task(self.collector._live_messages('-1001', [message]))
        try:
            await asyncio.wait_for(asyncio.gather(sender_entered.wait(), reply_entered.wait()), .5)
        finally:
            release.set()
        await asyncio.wait_for(live, .5)
        self.assertEqual('原始上下文', self.backend.calls[0]['reply_context']['text'])

    async def test_live_messages_reuse_recent_checkpoint_verification(self):
        self.collector.sources = {'-1001': self.backend.source_items[0]}

        await self.collector._live_messages('-1001', [Message(101)])
        await self.collector._live_messages('-1001', [Message(102)])
        self.assertEqual(1, len(self.backend.checkpoints))

        self.collector.checkpoint_verified_at['-1001'] -= 31
        await self.collector._live_messages('-1001', [Message(103)])
        self.assertEqual(2, len(self.backend.checkpoints))
        self.assertEqual(3, len(self.backend.calls))

    async def test_slow_directory_refresh_does_not_hold_live_message_lock(self):
        self.collector.client = self.client
        self.collector.sources = {'-1001': self.backend.source_items[0]}
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_directory(*_args):
            entered.set()
            await release.wait()

        self.collector._refresh_directory = slow_directory
        history = asyncio.create_task(self.collector._sync_source('-1001', self.backend.source_items[0]))
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.wait_for(self.collector._live_messages('-1001', [Message(99)]), timeout=.5)
        self.assertIn(('-1001', '99'), [(item['chat_id'], item['platform_message_id']) for item in self.backend.calls])
        release.set()
        await history

    async def test_slow_history_event_persistence_does_not_hold_live_message_lock(self):
        self.collector.client = self.client
        self.collector.sources = {'-1001': self.backend.source_items[0]}
        self.client.messages['-1001'] = [Message(1)]
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.backend.event

        async def slow_history_event(body):
            if body['platform_message_id'] == '1':
                entered.set()
                await release.wait()
            return await original(body)

        self.backend.event = slow_history_event
        history = asyncio.create_task(self.collector._sync_source('-1001', self.backend.source_items[0]))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            await asyncio.wait_for(self.collector._live_messages('-1001', [Message(99)]), timeout=.5)
            self.assertIn(('-1001', '99'), [(item['chat_id'], item['platform_message_id']) for item in self.backend.calls])
        finally:
            release.set()
        await asyncio.wait_for(history, timeout=1)

    async def test_history_time_slice_yields_only_after_a_durable_message(self):
        self.client.messages['-1001'] = [Message(1), Message(2)]
        with patch('collector.telegram.SYNC_HISTORY_SLICE_SECONDS', 0):
            result = await self.collector.sync_once()
        self.assertEqual('catching_up', result['sources']['-1001']['state'])
        self.assertEqual(1, self.backend.positions['-1001'])
        await self.collector.sync_once()
        self.assertEqual(2, self.backend.positions['-1001'])

    async def test_one_source_failure_does_not_block_other_source(self):
        self.backend.source_items.append({"chat_id": "-1002", "trusted_sender_ids": ["7"], "allow_media": True})
        self.client.messages = {"-1001": [Message(1)], "-1002": [Message(3)]}
        self.backend.fail_chat = "-1001"
        result = await self.collector.sync_once()
        self.assertEqual("unavailable", result["sources"]["-1001"]["state"])
        self.assertEqual(3, self.backend.positions["-1002"])

    async def test_flood_wait_only_backs_off_affected_source(self):
        self.backend.source_items.append({"chat_id": "-1002", "trusted_sender_ids": ["7"], "allow_media": True})
        self.client.messages = {"-1001": [Message(1)], "-1002": [Message(3)]}
        original = self.client.iter_messages

        async def stream(entity, **kwargs):
            if str(utils.get_peer_id(entity)) == "-1001":
                raise FloodWaitError(None, capture=60)
            async for message in original(entity, **kwargs):
                yield message

        self.client.iter_messages = stream
        result = await self.collector.sync_once()
        self.assertEqual("flood_wait", result["sources"]["-1001"]["state"])
        self.assertEqual(3, self.backend.positions["-1002"])

    async def test_untrusted_and_empty_messages_advance_without_review(self):
        self.client.messages["-1001"] = [Message(1, sender=8), Message(2, text="")]
        await self.collector.sync_once()
        self.assertEqual(2, self.backend.positions["-1001"])
        self.assertEqual({}, self.backend.events)

    async def test_trusted_channel_sender_keeps_channel_identity_instead_of_inventing_anonymous_person(self):
        sender_id = -1001358434454
        self.backend.source_items[0]["trusted_sender_ids"] = [str(sender_id)]
        message = Message(1, sender=sender_id)

        async def actual_sender():
            return Channel(id=1358434454, title="频道发言身份", photo=ChatPhotoEmpty(), date=datetime.now(timezone.utc))

        message.get_sender = actual_sender
        self.client.messages["-1001"] = [message]
        await self.collector.sync_once()
        event = next(iter(self.backend.events.values()))
        self.assertEqual(str(sender_id), event["sender_id"])
        self.assertEqual("频道发言身份", event["sender_nickname"])

    async def test_failed_media_is_persisted_then_retry_recovers(self):
        message = Message(1, media=True)
        self.client.messages["-1001"] = [message]
        attempts = []

        async def store(chat, item):
            attempts.append(item.id)
            if len(attempts) <= 2:
                raise ConnectionError("test media unavailable")
            return {"object_key": "telegram/test/1", "mime_type": "image/jpeg", "sha256": "a" * 64, "bytes": 100}

        self.collector.media_store = store
        await self.collector.sync_once()
        self.assertEqual(1, self.backend.positions["-1001"])
        self.assertEqual("failed", next(iter(self.backend.events.values()))["media_state"])
        self.assertEqual("阿森纳 全场大2.5", next(iter(self.backend.events.values()))["raw_text"])
        await self.collector.sync_once()
        self.assertEqual("failed", next(iter(self.backend.events.values()))["media_state"])
        await self.collector.sync_once()
        self.assertEqual("ready", next(iter(self.backend.events.values()))["media_state"])
        self.assertEqual(1, len(self.backend.events))

    async def test_album_has_all_images_caption_reply_and_one_version(self):
        first = Message(10, text="比赛信息", media=True, grouped=77)
        second = Message(11, text="主队 -0.5", media=True, grouped=77)
        second.reply_to_msg_id = 5
        self.client.messages["-1001"] = [first, second]

        async def store(chat, item):
            return {"object_key": f"telegram/test/{item.id}", "mime_type": "image/jpeg", "sha256": "a" * 64, "bytes": 100}

        self.collector.media_store = store
        await self.collector.sync_once()
        self.assertEqual(1, len(self.backend.events))
        event = next(iter(self.backend.events.values()))
        self.assertEqual("album:77", event["platform_message_id"])
        self.assertEqual([10, 11], event["member_message_ids"])
        self.assertEqual("比赛信息\n\n主队 -0.5", event["raw_text"])
        self.assertEqual("5", event["reply_to_platform_message_id"])
        self.assertEqual(2, len(event["media_objects"]))

    async def test_edit_generates_distinct_immutable_version(self):
        message = Message(1)
        self.client.messages["-1001"] = [message]
        await self.collector.sync_once()
        message.message = "阿森纳 全场小2.5"
        message.edit_date = message.date + timedelta(minutes=5)
        await self.collector._live_messages("-1001", [message])
        self.assertEqual(2, len(self.backend.events))
        self.assertEqual({"message", "edit"}, {value["event_kind"] for value in self.backend.events.values()})

    async def test_unsupported_media_saved_without_upload(self):
        self.client.messages["-1001"] = [Message(1, text="看视频", media=True, mime="video/mp4")]
        await self.collector.sync_once()
        self.assertEqual("unsupported", next(iter(self.backend.events.values()))["media_state"])
        self.assertEqual(1, self.backend.positions["-1001"])

    async def test_disconnected_old_edit_is_found_by_remote_reconciliation(self):
        message = Message(1)
        self.client.messages["-1001"] = [message]
        await self.collector.sync_once()
        old = next(iter(self.backend.events.values()))
        self.backend.observed_items = [{"content": old}]
        message.message = "断线期间修改为主队-0.5"
        message.edit_date = message.date + timedelta(minutes=1)
        await self.collector.sync_once()
        self.assertEqual(2, len(self.backend.events))
        self.assertEqual(1, self.backend.positions["-1001"])

    async def test_disconnected_deletion_is_found_without_new_message(self):
        self.client.messages["-1001"] = [Message(1)]
        await self.collector.sync_once()
        old = next(iter(self.backend.events.values()))
        self.backend.observed_items = [{"content": old}]
        self.client.messages["-1001"] = []
        await self.collector.sync_once()
        self.assertEqual("delete", self.backend.calls[-1]["event_kind"])

    async def test_stream_limits_and_image_format_validation(self):
        self.collector.client = self.client
        self.client.download_chunks = [b"x" * (MAX_MEDIA_BYTES + 1)]
        with self.assertRaisesRegex(ValueError, "media_size_limit"):
            await self.collector._store_media("-1001", Message(1, media=True))
        self.client.download_chunks = [b"<html>error</html>"]
        with self.assertRaisesRegex(ValueError, "image_format_mismatch"):
            await self.collector._store_media("-1001", Message(1, media=True))
        self.assertTrue(_valid_image(b"\x89PNG\r\n\x1a\nrest", "image/png"))

    async def test_decoded_pixels_and_corrupt_image_never_reach_storage(self):
        image = BytesIO()
        Image.new('RGB', (48, 32), 'blue').save(image, format='PNG')
        self.assertEqual({'width':48,'height':32,'frames':1}, _image_dimensions(image.getvalue(), 'image/png'))
        with patch('collector.telegram.MAX_IMAGE_PIXELS', 100):
            with self.assertRaisesRegex(ValueError, 'image_pixel_limit'):
                _image_dimensions(image.getvalue(), 'image/png')
        self.collector.client = self.client
        self.client.download_chunks = [b'\x89PNG\r\n\x1a\nrest']
        with self.assertRaises(ValueError):
            await self.collector._store_media('-1001', Message(1, media=True, mime='image/png'))

    async def test_new_message_media_uses_v2_storage_prefix(self):
        image = BytesIO()
        Image.new('RGB', (8, 8), 'blue').save(image, format='PNG')
        self.collector.client = self.client
        self.client.download_chunks = [image.getvalue()]
        uploads = []

        class S3:
            def put_object(inner, **kwargs):
                uploads.append(kwargs)

            def close(inner):
                pass

        env = {'S3_BUCKET':'test-private','S3_ENDPOINT_URL':'https://invalid.example',
               'S3_ACCESS_KEY':'test-only','S3_SECRET_KEY':'test-only'}
        with patch.dict(os.environ, env), patch('collector.telegram.boto3.client', return_value=S3()):
            record = await self.collector._store_media('-1001', Message(3, media=True, mime='image/png'))

        expected_prefix = 'v2/telegram/messages/-1001/3/'
        self.assertTrue(record['object_key'].startswith(expected_prefix))
        self.assertEqual(record['object_key'], uploads[0]['Key'])

    async def test_control_authentication_and_password_never_persisted(self):
        await self.collector.start_control_server(token="test-internal-token", host="127.0.0.1", port=0)
        port = self.collector.control_server.server.server_address[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            self.assertEqual(401, (await client.post("/status", json={})).status_code)
            response = await client.post("/status", json={}, headers={"Authorization": "Bearer test-internal-token"})
            self.assertEqual(200, response.status_code)
            self.assertEqual("idle", response.json()["auth"]["state"])
        self.assertEqual([], self.backend.saved_sessions)

    async def test_qr_cancel_and_two_step_password(self):
        self.client.qr = QR(self.client, needs_password=True)
        self.client.authorized = False
        state = await self.collector.control("/auth/qr", {})
        await asyncio.sleep(0.01)
        self.assertEqual("waiting_password", self.collector.auth_state["state"])
        bad = {"attempt_id": state["attempt_id"], "password": "wrong"}
        result = await self.collector.control("/auth/password", bad)
        self.assertEqual("waiting_password", result["state"])
        self.assertNotIn("password", bad)
        body = {"attempt_id": state["attempt_id"], "password": "test-correct-password"}
        with patch("collector.telegram.StringSession.save", return_value="test-session-only"):
            result = await self.collector.control("/auth/password", body)
        self.assertEqual("authorized", result["state"])
        self.assertEqual(1, len(self.backend.saved_sessions))
        self.assertNotIn("password", body)
        self.assertNotIn("password", repr(self.collector.auth_state))

    async def test_qr_cancel_and_stale_password_attempt_rejected(self):
        self.client.qr = QR(self.client)
        state = await self.collector.control("/auth/qr", {})
        repeated = await self.collector.control("/auth/qr", {})
        self.assertEqual(state["attempt_id"], repeated["attempt_id"])
        conflict = await self.collector.control("/auth/password", {"attempt_id": "stale", "password": "not-sent"})
        self.assertEqual("conflict", conflict["state"])
        result = await self.collector.control("/auth/cancel", {"attempt_id": state["attempt_id"]})
        self.assertEqual("cancelled", result["state"])
        self.assertIsNone(self.collector.auth_client)

    async def test_qr_expiry_releases_temporary_client(self):
        self.client.qr = QR(self.client)
        await self.collector.control("/auth/qr", {})
        self.collector.auth_deadline = 0
        status = await self.collector.control("/status", {})
        self.assertEqual("expired", status["auth"]["state"])
        self.assertIsNone(self.collector.auth_client)

    async def test_administrator_pagination_reaches_beyond_first_fifty(self):
        first = await self.collector.control("/admins", {"chat_id": "-1001"})
        second = await self.collector.control("/admins", {"chat_id": "-1001", "cursor": first["next_cursor"]})
        third = await self.collector.control("/admins", {"chat_id": "-1001", "cursor": second["next_cursor"]})
        self.assertEqual(105, sum(len(page["items"]) for page in (first, second, third)))
        self.assertIsNone(third["next_cursor"])
        self.assertTrue(first["manual_id_allowed"])

    async def test_authorized_session_not_claimed_until_remote_save(self):
        self.client.qr = QR(self.client, immediate=True)
        self.backend.save_fails = True
        with patch("collector.telegram.StringSession.save", return_value="test-session-only"):
            await self.collector.control("/auth/qr", {})
            await asyncio.sleep(0.01)
            self.assertEqual("authorized_pending_save", self.collector.auth_state["state"])
            self.backend.save_fails = False
            await self.collector.sync_once()
        self.assertEqual("authorized", self.collector.auth_state["state"])
        self.assertEqual(1, len(self.backend.saved_sessions))

    async def test_revoked_saved_session_clears_identity_and_waits_for_config_change(self):
        self.collector.config["telegram_account"] = json.dumps({"id": "1234", "display_name": "旧账号"})
        self.client.authorized = False
        self.assertFalse(await self.collector.connect())
        self.assertEqual("session_invalid", self.collector.auth_state["state"])
        self.assertIsNone(self.collector.account)
        status = await self.collector.control("/status", {})
        self.assertFalse(status["authorized"])
        self.assertIsNone(status["account"])
        self.assertFalse(await self.collector.connect())
        self.assertEqual(1, len(self.created))

        self.client.authorized = True
        self.client.account_id = 5678
        await self.collector.apply_config(CONFIG | {"telegram_session": "replacement-session-only",
                                                    "telegram_account": json.dumps({"id": "5678", "display_name": "新账号"})})
        self.assertTrue(await self.collector.connect())
        self.assertEqual("5678", self.collector.account["id"])
        self.assertEqual("authorized", self.collector.auth_state["state"])

    async def test_duplicated_auth_key_invalidates_session_and_stops_retries(self):
        self.collector.config["telegram_account"] = json.dumps({"id": "1234", "display_name": "旧账号"})
        self.client.authorization_error = AuthKeyDuplicatedError(None)
        with self.assertRaises(AuthKeyDuplicatedError):
            await self.collector.connect()
        self.assertEqual("session_invalid", self.collector.auth_state["state"])
        self.assertIsNone(self.collector.account)
        self.assertFalse(self.client.is_connected())
        self.assertFalse(await self.collector.connect())
        self.assertEqual(1, self.client.connect_count)
        self.assertEqual(1, len(self.created))

    async def test_duplicated_auth_key_during_source_fetch_invalidates_connected_session(self):
        self.assertTrue(await self.collector.connect())
        self.client.request_error = AuthKeyDuplicatedError(None)
        result = await self.collector.sync_once()
        self.assertEqual("AuthKeyDuplicatedError", result["sources"]["-1001"]["error"])
        self.assertEqual("session_invalid", self.collector.auth_state["state"])
        self.assertIsNone(self.collector.client)
        self.assertEqual(1, self.client.disconnect_count)
        self.assertFalse(await self.collector.connect())
        self.assertEqual(1, self.client.connect_count)
        self.assertEqual(1, len(self.created))

    async def test_connected_session_revocation_drops_client_and_account(self):
        self.collector.config["telegram_account"] = json.dumps({"id": "1234", "display_name": "旧账号"})
        self.assertTrue(await self.collector.connect())
        self.assertIs(self.client, self.collector.client)
        self.client.authorized = False
        self.assertFalse(await self.collector.connect())
        self.assertIsNone(self.collector.client)
        self.assertIsNone(self.collector.account)
        self.assertEqual("session_invalid", self.collector.auth_state["state"])
        self.assertEqual(1, len(self.created))

    async def test_saved_session_and_account_survive_collector_restart(self):
        self.client.qr = QR(self.client, immediate=True)
        with patch("collector.telegram.StringSession.save", return_value="test-session-only"):
            await self.collector.control("/auth/qr", {})
            await asyncio.sleep(0.01)
        saved_session, saved_account = self.backend.saved_sessions[0]
        restored_client = Client()
        restored = TelegramCollector(CONFIG | {"telegram_session": saved_session,
                                                "telegram_account": json.dumps(saved_account)},
                                     backend=self.backend, client_factory=lambda *args: restored_client)
        try:
            self.assertTrue(await restored.connect())
            status = await restored.control("/status", {})
            self.assertTrue(status["authorized"])
            self.assertEqual("1234", status["account"]["id"])
            self.assertEqual("authorized", status["auth"]["state"])
        finally:
            await restored.disconnect()

    async def test_orphan_media_cleanup_deletes_and_acknowledges_only_successful_objects(self):
        deleted = []
        self.backend.orphan_items = [
            {"object_key": "telegram/messages/-1001/1/" + "a" * 64},
            {"object_key": "telegram/messages/-1001/2/" + "b" * 64},
        ]

        async def remove(key):
            deleted.append(key)
            if key.endswith("b" * 64):
                raise ConnectionError("storage unavailable")

        self.collector.media_delete = remove
        result = await self.collector.cleanup_orphan_media(older_than_hours=24, limit=10)
        self.assertEqual({"state": "ready", "candidates": 2, "deleted": 1}, result)
        self.assertEqual([self.backend.orphan_items[0]["object_key"], self.backend.orphan_items[1]["object_key"]], deleted)
        self.assertEqual([[self.backend.orphan_items[0]["object_key"]]], [item[1] for item in self.backend.orphan_acks])

    async def test_status_snapshot_does_not_return_self_referential_worker_health(self):
        health = {"telegram": {"state": "connected"}}
        health["source_health"] = health
        self.collector.source_health = health
        status = await self.collector.control("/status", {})
        encoded = json.dumps(status, ensure_ascii=False)
        self.assertIn("connected", encoded)
        self.assertNotIn("source_health", status["source_health"])


if __name__ == "__main__":
    unittest.main()
