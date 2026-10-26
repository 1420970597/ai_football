import asyncio
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from telethon import utils
from telethon.errors import FloodWaitError, PasswordHashInvalidError, SessionPasswordNeededError
from telethon.tl.types import Channel, Chat, ChatPhotoEmpty, ChatParticipantAdmin, ChatParticipants
from telethon.tl.functions.messages import GetFullChatRequest

from collector.telegram import TelegramCollector, MAX_MEDIA_BYTES, _valid_image


CONFIG = {"telegram_api_id": "12345", "telegram_api_hash": "test-api-hash-only",
          "telegram_session": "test-session-only", "telegram_source_enabled": "true"}


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
        self.source_items = [{"chat_id": "-1001", "trusted_sender_ids": ["7"], "allow_media": True}]
        self.fail_chat = None
        self.save_fails = False
        self.saved_sessions = []
        self.observed_items = []

    async def sources(self):
        return self.source_items

    async def checkpoint(self, chat_id, owner):
        return {"last_message_id": self.positions.get(chat_id, 0)}

    async def advance(self, chat_id, owner, last_message_id, previous):
        if self.positions.get(chat_id, 0) != previous:
            raise RuntimeError("checkpoint mismatch")
        self.positions[chat_id] = last_message_id

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
        return self.observed_items


class Client:
    def __init__(self, messages=None):
        self.messages = messages or {"-1001": []}
        self.connected, self.authorized = False, True
        self.connect_count, self.disconnect_count = 0, 0
        self.handlers = []
        self.query_limits = []
        self.session = object()
        self.qr = None
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
        return self.authorized

    def add_event_handler(self, fn, event):
        self.handlers.append((fn, event))

    async def get_entity(self, value):
        return Chat(id=abs(int(value)), title="测试群", photo=ChatPhotoEmpty(), participants_count=105,
                    date=datetime(2026, 10, 26, tzinfo=timezone.utc), version=1)

    async def __call__(self, request):
        if isinstance(request, GetFullChatRequest):
            users = [SimpleNamespace(id=index, first_name=f"专家{index}", last_name="", username=None) for index in range(1, 106)]
            admins = [ChatParticipantAdmin(user_id=user.id, inviter_id=1, date=datetime.now(timezone.utc)) for user in users]
            return SimpleNamespace(full_chat=SimpleNamespace(participants=ChatParticipants(request.chat_id, admins, 1)), users=users)
        raise AssertionError("unexpected fake RPC")

    async def iter_messages(self, entity, min_id, reverse, limit):
        self.query_limits.append(limit)
        for message in sorted(self.messages.get(str(utils.get_peer_id(entity)), []), key=lambda item: item.id):
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
        return SimpleNamespace(id=1234, first_name="测试", last_name="账号", username="test-account")

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
        result = await self.collector.sync_once()
        self.assertEqual(105, result["sources"]["-1001"]["observed"])
        self.assertEqual(105, len(self.backend.events))
        self.assertEqual(105, self.backend.positions["-1001"])
        self.assertEqual([None], self.client.query_limits)
        await self.collector.sync_once()
        self.assertEqual(1, self.client.connect_count)
        self.assertEqual(1, len(self.created))

    async def test_persistence_failure_does_not_advance_or_mark_seen(self):
        self.client.messages["-1001"] = [Message(1)]
        self.backend.fail_chat = "-1001"
        await self.collector.sync_once()
        self.assertEqual(0, self.backend.positions.get("-1001", 0))
        self.backend.fail_chat = None
        await self.collector.sync_once()
        self.assertEqual(1, self.backend.positions["-1001"])
        self.assertEqual(1, len(self.backend.events))

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


if __name__ == "__main__":
    unittest.main()
