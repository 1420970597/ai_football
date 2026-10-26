from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import re
import time
from typing import Any
import uuid

import boto3
from botocore.config import Config
from telethon import TelegramClient, events, utils
from telethon.errors import FloodWaitError, SessionPasswordNeededError, PasswordHashInvalidError
from telethon.sessions import StringSession
from telethon.tl.types import (Channel, ChannelForbidden, ChannelParticipantsAdmins,
                              ChannelParticipantAdmin, ChannelParticipantCreator, Chat, ChatForbidden,
                              ChatParticipantAdmin, ChatParticipantCreator, ChatParticipantsForbidden,
                              UserProfilePhotoEmpty)
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.functions.messages import GetFullChatRequest

from .telegram_backend import TelegramBackend
from .telegram_control import ControlServer

log = logging.getLogger("platform.telegram")
MAX_MEDIA_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_MEDIA_BYTES = 32 * 1024 * 1024
MAX_MEDIA_COUNT = 10
MAX_IMAGE_PIXELS = 40_000_000
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MESSAGE_MEDIA_PREFIX = "telegram/messages"
MESSAGE_MEDIA_RETENTION = "expert_message_evidence"


class SourceAccessError(RuntimeError):
    def __init__(self, reason, **details):
        super().__init__(reason)
        self.details = details


def config_int(value) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _utc(value):
    if value.tzinfo is None:
        raise ValueError("Telegram timestamp must include timezone")
    return value.astimezone(timezone.utc)


def _factory(session: str, api_id: int, api_hash: str):
    # StringSession 只使用内存，不创建本地 SQLite。
    return TelegramClient(StringSession(session), api_id, api_hash, connection_retries=3,
                          retry_delay=2, request_retries=2, flood_sleep_threshold=0)


def _nickname(sender, sender_id: str) -> str:
    return (" ".join(filter(None, [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")]))
            or getattr(sender, "title", "") or getattr(sender, "username", "") or sender_id)[:120]


def _valid_image(body: bytes, mime: str) -> bool:
    if mime == "image/jpeg":
        return body.startswith(b"\xff\xd8\xff")
    if mime == "image/png":
        return body.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/gif":
        return body.startswith((b"GIF87a", b"GIF89a"))
    return mime == "image/webp" and body.startswith(b"RIFF") and body[8:12] == b"WEBP"


class TelegramCollector:
    def __init__(self, config: dict[str, str], *, api=None, api_url: str | None = None,
                 backend=None, client_factory=None, media_store=None):
        self.config = dict(config)
        self.backend = backend or (TelegramBackend(api, api_url) if api is not None and api_url else None)
        self.client_factory = client_factory or _factory
        self.media_store = media_store
        self.client = None
        self.owner = str(uuid.uuid4())
        self.sources: dict[str, dict] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.retry_at: dict[str, float] = {}
        self.control_server = None
        self.auth_client = None
        self.auth_task = None
        self.auth_state: dict[str, Any] = {"state": "idle"}
        self.auth_deadline = 0.0
        self.account = None
        self.invalid_session_fingerprint = None

    @staticmethod
    def _saved_account(config):
        try:
            account = json.loads(str(config.get("telegram_account", "")))
        except (TypeError, ValueError):
            return None
        if not isinstance(account, dict) or not account.get("id"):
            return None
        return {key: account.get(key) for key in ("id", "display_name", "username")}

    @staticmethod
    def _session_fingerprint(session):
        return hashlib.sha256(session.encode("utf-8")).digest()

    async def _mark_session_invalid(self, client, fingerprint):
        await client.disconnect()
        if self.client is client:
            self.client = None
        self.account = None
        self.invalid_session_fingerprint = fingerprint
        self.auth_state = {"state": "session_invalid"}

    def _new_client(self, session: str):
        api_id = config_int(self.config.get("telegram_api_id", ""))
        api_hash = str(self.config.get("telegram_api_hash", "")).strip()
        if not api_id or not api_hash:
            raise RuntimeError("Telegram API application is not configured")
        return self.client_factory(session, api_id, api_hash)

    async def connect(self) -> bool:
        if self.client:
            if not self.client.is_connected():
                await self.client.connect()
            if not await self.client.is_user_authorized():
                session = str(self.config.get("telegram_session", "")).strip()
                await self._mark_session_invalid(self.client, self._session_fingerprint(session))
                return False
            self.account = self._saved_account(self.config)
            self.auth_state = {"state": "authorized", "account": self.account} if self.account else {"state": "authorized"}
            return True
        session = str(self.config.get("telegram_session", "")).strip()
        if not session:
            return False
        fingerprint = self._session_fingerprint(session)
        if self.invalid_session_fingerprint == fingerprint:
            return False
        candidate = self._new_client(session)
        try:
            await candidate.connect()
            if not await candidate.is_user_authorized():
                await self._mark_session_invalid(candidate, fingerprint)
                return False
        except BaseException:
            await candidate.disconnect()
            raise
        self.client = candidate
        self.account = self._saved_account(self.config)
        self.auth_state = {"state": "authorized", "account": self.account} if self.account else {"state": "authorized"}
        self.invalid_session_fingerprint = None
        self._handlers(candidate)
        return True

    def _handlers(self, client):
        client.add_event_handler(self._on_message, events.NewMessage())
        client.add_event_handler(self._on_message, events.MessageEdited())
        client.add_event_handler(self._on_album, events.Album())
        client.add_event_handler(self._on_delete, events.MessageDeleted())

    async def apply_config(self, config: dict[str, str]):
        identity = ("telegram_api_id", "telegram_api_hash", "telegram_session")
        changed = any(str(self.config.get(key, "")) != str(config.get(key, "")) for key in identity)
        if changed and self.client:
            await self.client.disconnect()
            self.client = None
        if changed:
            self.account = None
            self.invalid_session_fingerprint = None
            self.auth_state = {"state": "idle" if str(config.get("telegram_session", "")).strip() else "disconnected"}
        self.config = dict(config)

    async def disconnect(self):
        await self._cancel_auth()
        if self.client:
            await self.client.disconnect()
            self.client = None
        if self.control_server:
            await self.control_server.close()
            self.control_server = None

    async def start_control_server(self, *, token: str, host="0.0.0.0", port=8092):
        if not self.control_server:
            self.control_server = ControlServer(self, token, host, port)
            self.control_server.start()

    async def _entity(self, chat_id: str):
        chat_id = str(chat_id).strip()
        if not (re.fullmatch(r"-[1-9][0-9]{0,18}", chat_id)
                or re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", chat_id)):
            raise SourceAccessError("invalid_source_identifier")
        if chat_id.startswith("-") and int(chat_id) < -(2 ** 63):
            raise SourceAccessError("invalid_source_identifier")
        value = int(chat_id) if chat_id.startswith("-") else chat_id
        try:
            entity = await self.client.get_entity(value)
        except ValueError:
            # StringSession 不持久化实体访问哈希：先从已加入对话解析，不能当成群组无管理员。
            async for dialog in self.client.iter_dialogs(limit=None):
                entity = dialog.entity
                if str(utils.get_peer_id(entity)) == chat_id or str(getattr(entity, "username", "")).casefold() == chat_id.lstrip("@").casefold():
                    return self._group_entity(entity)
            raise SourceAccessError("account_has_not_joined_or_entity_is_unavailable") from None
        return self._group_entity(entity)

    def _group_entity(self, entity):
        if isinstance(entity, (ChannelForbidden, ChatForbidden)):
            raise SourceAccessError("source_not_accessible")
        if not isinstance(entity, (Channel, Chat)):
            raise SourceAccessError("source_not_group_or_channel")
        if getattr(entity, "left", False) or getattr(entity, "deactivated", False):
            raise SourceAccessError("account_has_not_joined")
        migrated = getattr(entity, "migrated_to", None)
        if migrated:
            raise SourceAccessError("source_migrated", resolved_chat_id=str(utils.get_peer_id(migrated)))
        return entity

    async def sync_once(self) -> dict:
        if not self.backend:
            raise RuntimeError("remote Telegram backend is required")
        if self.auth_state.get("state") == "authorized_pending_save":
            await self._finish_auth()
        if str(self.config.get("telegram_source_enabled", "false")).lower() != "true":
            return {"state": "disabled", "sources": {}}
        if not await self.connect():
            return {"state": "unauthorized", "sources": {}}
        self.sources = {str(item["chat_id"]): item for item in await self.backend.sources()}
        result = {}
        # 群组之间独立，但控制并发和 Telegram flood；单群失败不阻止其他群。
        gate = asyncio.Semaphore(3)

        async def one(chat_id, source):
            async with gate:
                try:
                    result[chat_id] = await self._sync_source(chat_id, source)
                    if result[chat_id]["state"] != "backoff":
                        await self._reconcile_known(chat_id, source)
                except FloodWaitError as exc:
                    self.retry_at[chat_id] = time.monotonic() + exc.seconds
                    result[chat_id] = {"state": "flood_wait", "retry_after": exc.seconds}
                except Exception as exc:
                    result[chat_id] = {"state": "unavailable", "error": type(exc).__name__}
                    log.warning("Telegram source unavailable category=%s", type(exc).__name__)

        await asyncio.gather(*(one(chat_id, source) for chat_id, source in self.sources.items()))
        await self.retry_pending()
        return {"state": "connected", "sources": result}

    async def _sync_source(self, chat_id: str, source: dict) -> dict:
        if time.monotonic() < self.retry_at.get(chat_id, 0):
            return {"state": "backoff"}
        lock = self.locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            checkpoint = await self.backend.checkpoint(chat_id, self.owner)
            original = int(checkpoint["last_message_id"])
            last, count = original, 0
            entity = await self._entity(chat_id)
            async for message in self.client.iter_messages(entity, min_id=original, reverse=True, limit=None):
                if not message or int(message.id) <= last:
                    continue
                # 先持久原文/待媒体状态，确认后才推进高水位；不使用内存 seen 集合。
                await self._process_message(chat_id, source, message, entity)
                last, count = int(message.id), count + 1
                if count % 25 == 0:
                    await self.backend.advance(chat_id, self.owner, last, original)
                    original = last
                if count >= 1000:
                    break
            if last != original:
                await self.backend.advance(chat_id, self.owner, last, original)
            return {"state": "caught_up" if count < 1000 else "catching_up", "last_message_id": last, "observed": count}

    async def _reconcile_known(self, chat_id: str, source: dict):
        # 高水位只覆盖新消息；按远端调度轮转复核旧版本，补回断线期间的编辑和删除。
        async with self.locks.setdefault(chat_id, asyncio.Lock()):
            await self.backend.checkpoint(chat_id, self.owner)
            entity = await self._entity(chat_id)
            for item in await self.backend.observed(chat_id, self.owner):
                ids = item["content"]["member_message_ids"]
                members = [entry for entry in await self.client.get_messages(entity, ids=ids) if entry]
                missing = set(ids) - {entry.id for entry in members}
                if missing:
                    await self._delete(chat_id, sorted(missing))
                else:
                    await self._process_group(chat_id, source, sorted(members, key=lambda entry: entry.id))

    async def _expand_album(self, message, entity):
        grouped_id = getattr(message, "grouped_id", None)
        if not grouped_id:
            return [message]
        # 按组检索同一相册的成员，编辑单张图片也必须重新分析整个图文版本。
        ids = list(range(max(1, int(message.id) - 20), int(message.id) + 21))
        candidates = await self.client.get_messages(entity, ids=ids)
        members = [item for item in candidates if item and getattr(item, "grouped_id", None) == grouped_id]
        if not any(item.id == message.id for item in members):
            members.append(message)
        return sorted(members, key=lambda item: item.id)

    async def _process_message(self, chat_id: str, source: dict, message, entity=None):
        members = await self._expand_album(message, entity) if getattr(message, "grouped_id", None) else [message]
        return await self._process_group(chat_id, source, members)

    async def _process_group(self, chat_id: str, source: dict, members: list):
        message = members[0]
        sender_id = str(getattr(message, "sender_id", "") or "")
        if sender_id not in {str(value) for value in source.get("trusted_sender_ids", [])}:
            return {"state": "ignored_author"}
        sender = await message.get_sender()
        manifests = []
        raw_text = "\n\n".join(item.message for item in members if getattr(item, "message", None))
        reply = next((item.reply_to_msg_id for item in members if getattr(item, "reply_to_msg_id", None)), None)
        reply_context = {}
        if reply:
            try:
                replied = await message.get_reply_message()
                if replied:
                    reply_context = {"message_id": str(replied.id), "sender_id": str(getattr(replied, "sender_id", "") or ""),
                                     "text": (getattr(replied, "message", "") or "")[:20000],
                                     "has_media": bool(getattr(replied, "media", None))}
                else:
                    reply_context = {"unavailable": True}
            except Exception:
                reply_context = {"unavailable": True}
        for item in members:
            if getattr(item, "media", None):
                file = getattr(item, "file", None)
                manifests.append({"message_id": int(item.id), "mime_type": getattr(file, "mime_type", None) or "image/jpeg",
                                  "bytes": int(getattr(file, "size", 0) or 0),
                                  "width": int(getattr(file, "width", 0) or 0), "height": int(getattr(file, "height", 0) or 0)})
        if not raw_text.strip() and not manifests:
            return {"state": "ignored_empty"}
        version_at = max(_utc(getattr(item, "edit_date", None) or item.date) for item in members)
        grouped_id = getattr(message, "grouped_id", None)
        payload = {
            "chat_id": chat_id, "owner": self.owner,
            "platform_message_id": f"album:{grouped_id}" if grouped_id else str(message.id),
            "member_message_ids": [int(item.id) for item in members],
            "sender_id": sender_id, "sender_nickname": _nickname(sender, sender_id),
            "event_kind": "edit" if any(getattr(item, "edit_date", None) for item in members) else "message",
            "source_version_at": version_at.isoformat(), "posted_at": _utc(message.date).isoformat(),
            "raw_text": raw_text, "reply_to_platform_message_id": str(reply) if reply else None,
            "reply_context": reply_context,
            "media_manifest": manifests, "media_objects": [],
        }
        # 昵称变化不制造消息版本；后台按稳定作者编号独立更新展示名。
        fingerprint = {key: value for key, value in payload.items() if key not in {"owner", "sender_nickname", "media_objects"}}
        payload["version_key"] = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        supported = (len(manifests) <= MAX_MEDIA_COUNT and source.get("allow_media", True)
                     and all(item["mime_type"] in ALLOWED_IMAGE_TYPES and item["bytes"] <= MAX_MEDIA_BYTES
                             and item["width"] * item["height"] <= MAX_IMAGE_PIXELS for item in manifests)
                     and sum(item["bytes"] for item in manifests) <= MAX_MESSAGE_MEDIA_BYTES)
        payload["media_state"] = ("pending" if supported else "unsupported") if manifests else "ready"
        first = await self.backend.event(payload)
        if first.get("state") in {"ready", "superseded", "ignored_stale", "deleted"} or not manifests or not supported:
            return first
        objects = []
        try:
            used = 0
            for item in members:
                if getattr(item, "media", None):
                    stored = await self._store_media(chat_id, item)
                    used += int(stored["bytes"])
                    if used > MAX_MESSAGE_MEDIA_BYTES:
                        raise ValueError("message_media_size_limit")
                    objects.append(stored)
        except Exception as exc:
            payload.update(media_state="failed", last_error=type(exc).__name__, media_objects=[])
            await self.backend.event(payload)
            return {"persisted": True, "state": "failed"}
        payload.update(media_state="ready", media_objects=objects)
        return await self.backend.event(payload)

    async def retry_pending(self):
        if not self.client or not self.backend:
            return
        for item in await self.backend.pending():
            chat_id = str(item["chat_id"])
            source = self.sources.get(chat_id)
            if source is None:
                continue
            try:
                async with self.locks.setdefault(chat_id, asyncio.Lock()):
                    await self.backend.checkpoint(chat_id, self.owner)
                    entity = await self._entity(chat_id)
                    identifiers = item["content"]["member_message_ids"]
                    members = [entry for entry in await self.client.get_messages(entity, ids=identifiers) if entry]
                    missing = set(identifiers) - {entry.id for entry in members}
                    if missing:
                        await self._delete(chat_id, sorted(missing))
                    else:
                        await self._process_group(chat_id, source, sorted(members, key=lambda entry: entry.id))
            except FloodWaitError as exc:
                self.retry_at[chat_id] = time.monotonic() + exc.seconds
            except Exception as exc:
                log.warning("Telegram media retry deferred category=%s", type(exc).__name__)

    async def _on_message(self, event):
        if getattr(event.message, "grouped_id", None):
            # 新相册由 Album 回调完整聚合；编辑相册仍需读取全组。
            if isinstance(event, events.NewMessage.Event) and not getattr(event.message, "edit_date", None):
                return
        await self._live_messages(str(event.chat_id), [event.message])

    async def _on_album(self, event):
        await self._live_messages(str(event.chat_id), list(event.messages), full_album=True)

    async def _live_messages(self, chat_id, members, full_album=False):
        source = self.sources.get(chat_id)
        if not source:
            return
        try:
            async with self.locks.setdefault(chat_id, asyncio.Lock()):
                await self.backend.checkpoint(chat_id, self.owner)
                if full_album:
                    await self._process_group(chat_id, source, sorted(members, key=lambda item: item.id))
                else:
                    entity = await self._entity(chat_id)
                    await self._process_message(chat_id, source, members[0], entity)
        except Exception as exc:
            # 不移动高水位，周期补拉负责重新投递失败的新消息。
            log.warning("Telegram live event deferred category=%s", type(exc).__name__)

    async def _delete(self, chat_id: str, identifiers: list[int]):
        now = datetime.now(timezone.utc).isoformat()
        key = hashlib.sha256(json.dumps(sorted(identifiers)).encode()).hexdigest()
        return await self.backend.event({"chat_id": chat_id, "owner": self.owner,
            "platform_message_id": "delete:" + key, "member_message_ids": identifiers, "version_key": key,
            "event_kind": "delete", "source_version_at": now, "posted_at": now,
            "media_state": "ready", "raw_text": "", "media_manifest": [], "media_objects": []})

    async def _on_delete(self, event):
        chat_id = str(getattr(event, "chat_id", "") or "")
        if chat_id not in self.sources:
            return
        try:
            async with self.locks.setdefault(chat_id, asyncio.Lock()):
                await self.backend.checkpoint(chat_id, self.owner)
                await self._delete(chat_id, [int(value) for value in event.deleted_ids])
        except Exception as exc:
            log.warning("Telegram deletion unavailable category=%s", type(exc).__name__)

    async def _store_media(self, chat: str, message) -> dict:
        if self.media_store:
            return await self.media_store(chat, message)
        mime = getattr(getattr(message, "file", None), "mime_type", None) or "image/jpeg"
        if mime not in ALLOWED_IMAGE_TYPES:
            raise ValueError("unsupported_media")
        chunks, size = [], 0
        async for chunk in self.client.iter_download(message.media, request_size=256 * 1024):
            size += len(chunk)
            if size > MAX_MEDIA_BYTES:
                raise ValueError("media_size_limit")
            chunks.append(bytes(chunk))
        body = b"".join(chunks)
        if not body or not _valid_image(body, mime):
            raise ValueError("image_format_mismatch")
        digest = hashlib.sha256(body).hexdigest()
        # Keep message media in a dedicated namespace.  Avatar and future
        # review-evidence objects must never share this prefix, so retention
        # jobs can operate on each class without guessing from a key.
        key = f"{MESSAGE_MEDIA_PREFIX}/{chat}/{message.id}/{digest}"
        bucket = os.environ.get("S3_BUCKET", "").strip()
        endpoint = os.environ.get("S3_ENDPOINT_URL", "").strip()
        access = os.environ.get("S3_ACCESS_KEY", "").strip()
        secret = os.environ.get("S3_SECRET_KEY", "")
        if not all((bucket, endpoint, access, secret)):
            raise RuntimeError("remote_S3_not_configured")

        def upload():
            client = boto3.client("s3", endpoint_url=endpoint, region_name=os.environ.get("S3_REGION", "us-east-1"),
                                  aws_access_key_id=access, aws_secret_access_key=secret,
                                  use_ssl=os.environ.get("S3_USE_TLS", "true").lower() == "true",
                                  config=Config(connect_timeout=3, read_timeout=15, retries={"max_attempts": 2},
                                                proxies={"https": os.environ["S3_HTTPS_PROXY"]} if os.environ.get("S3_HTTPS_PROXY") else None))
            try:
                client.put_object(
                    Bucket=bucket, Key=key, Body=body, ContentType=mime,
                    Metadata={"sha256": digest, "purpose": "expert-message", "retention": MESSAGE_MEDIA_RETENTION},
                )
            finally:
                client.close()
        await asyncio.to_thread(upload)
        return {"object_key": key, "mime_type": mime, "sha256": digest, "bytes": size,
                "purpose": "expert-message", "retention": MESSAGE_MEDIA_RETENTION}

    async def _cancel_auth(self):
        task, self.auth_task = self.auth_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.auth_client:
            await self.auth_client.disconnect()
            self.auth_client = None
        if self.auth_state.get("state") not in {"idle", "authorized", "cancelled"}:
            self.auth_state = {"state": "cancelled", "attempt_id": self.auth_state.get("attempt_id")}

    async def _start_qr(self, replace=False):
        if self.auth_state.get("state") in {"waiting_qr", "waiting_password", "authorized_pending_save"} and not replace:
            return dict(self.auth_state)
        await self._cancel_auth()
        self.auth_client = self._new_client("")
        await self.auth_client.connect()
        qr = await self.auth_client.qr_login()
        self.auth_deadline = time.monotonic() + 900
        self.auth_state = {"state": "waiting_qr", "attempt_id": str(uuid.uuid4()), "qr_url": qr.url,
                           "expires_at": _utc(qr.expires).isoformat()}
        self.auth_task = asyncio.create_task(self._wait_qr(qr), name="telegram-qr-login")
        return dict(self.auth_state)

    async def _wait_qr(self, qr):
        try:
            while time.monotonic() < self.auth_deadline:
                remaining = max(1, (_utc(qr.expires) - datetime.now(timezone.utc)).total_seconds())
                try:
                    await qr.wait(timeout=min(remaining, 120))
                    await self._finish_auth()
                    return
                except asyncio.TimeoutError:
                    await qr.recreate()
                    self.auth_state.update(qr_url=qr.url, expires_at=_utc(qr.expires).isoformat())
                except SessionPasswordNeededError:
                    self.auth_state = {"state": "waiting_password", "attempt_id": self.auth_state["attempt_id"], "attempts": 0}
                    return
            self.auth_state = {"state": "expired", "attempt_id": self.auth_state["attempt_id"]}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.auth_state.get("state") == "authorized_pending_save":
                self.auth_state["error"] = type(exc).__name__
            else:
                self.auth_state = {"state": "failed", "attempt_id": self.auth_state.get("attempt_id"), "error": type(exc).__name__}

    async def _finish_auth(self):
        if not self.auth_client:
            return
        account = await self.auth_client.get_me()
        info = {"id": str(account.id), "display_name": _nickname(account, str(account.id)), "username": getattr(account, "username", None)}
        session = StringSession.save(self.auth_client.session)
        self.auth_state = {"state": "authorized_pending_save", "attempt_id": self.auth_state.get("attempt_id")}
        if not self.backend:
            raise RuntimeError("remote Telegram backend is required")
        await self.backend.save_session(session, info)
        if self.client and self.client is not self.auth_client:
            await self.client.disconnect()
        self.client, self.auth_client = self.auth_client, None
        self.config["telegram_session"] = session
        self.config["telegram_account"] = json.dumps(info, ensure_ascii=False)
        self.config["telegram_source_enabled"] = "true"
        self.account = info
        self.invalid_session_fingerprint = None
        self._handlers(self.client)
        self.auth_state = {"state": "authorized", "attempt_id": self.auth_state.get("attempt_id"), "account": info}

    async def control(self, path: str, payload: dict):
        if self.auth_state.get("state") in {"waiting_qr", "waiting_password"} and time.monotonic() >= self.auth_deadline:
            await self._cancel_auth()
            self.auth_state["state"] = "expired"
        if path == "/status":
            return {"connected": bool(self.client and self.client.is_connected()),
                    "authorized": bool(self.client and await self.client.is_user_authorized()),
                    "auth": dict(self.auth_state), "account": self.account,
                    "source_health": getattr(self, "source_health", None)}
        if path == "/auth/qr":
            return await self._start_qr(bool(payload.get("replace_existing")))
        if path in {"/auth/password", "/auth/cancel"}:
            if payload.get("attempt_id") != self.auth_state.get("attempt_id"):
                return {"state": "conflict", "reason": "authorization_attempt_changed"}
            if path == "/auth/cancel":
                await self._cancel_auth()
                return dict(self.auth_state)
            if self.auth_state.get("state") != "waiting_password" or not self.auth_client:
                return {"state": "conflict", "reason": "password_not_requested"}
            password = payload.pop("password", "")
            if not isinstance(password, str) or not 1 <= len(password) <= 256:
                return {"state": "waiting_password", "reason": "password_required"}
            try:
                await self.auth_client.sign_in(password=password)
            except PasswordHashInvalidError:
                self.auth_state["attempts"] += 1
                if self.auth_state["attempts"] >= 5:
                    await self._cancel_auth()
                return dict(self.auth_state) | {"reason": "invalid_password"}
            finally:
                password = None
            await self._finish_auth()
            return dict(self.auth_state)
        if path == "/disconnect":
            await self._cancel_auth()
            if self.client:
                try:
                    await self.client.log_out()
                finally:
                    await self.client.disconnect()
                    self.client = None
            self.config["telegram_session"] = ""
            self.config["telegram_source_enabled"] = "false"
            self.config["telegram_account"] = ""
            self.account = None
            self.invalid_session_fingerprint = None
            self.auth_state = {"state": "disconnected"}
            return {"state": "disconnected"}
        if path in {"/dialogs", "/admins"}:
            try:
                if not await self.connect():
                    return {"items": [], "state": "unauthorized", "manual_id_allowed": False}
                return await (self._dialogs(payload) if path == "/dialogs" else self._admins(payload))
            except FloodWaitError as exc:
                return {"items": [], "state": "flood_wait", "retry_after": exc.seconds}
            except Exception as exc:
                reason = {"ChatAdminRequiredError": "member_enumeration_restricted", "ChannelPrivateError": "source_not_accessible",
                          "UserNotParticipantError": "account_has_not_joined", "ChannelInvalidError": "invalid_source_identifier",
                          "PeerIdInvalidError": "invalid_source_identifier", "UsernameInvalidError": "invalid_source_identifier",
                          "UsernameNotOccupiedError": "invalid_source_identifier"}.get(type(exc).__name__, "upstream_unavailable")
                if isinstance(exc, SourceAccessError):
                    reason = str(exc)
                if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
                    reason = "network_unavailable"
                return {"items": [], "state": "unavailable", "reason": reason,
                        "manual_id_allowed": path == "/admins" and reason == "member_enumeration_restricted",
                        **(exc.details if isinstance(exc, SourceAccessError) else {})}
        return {"state": "unsupported"}

    async def _dialogs(self, payload):
        offset = self._offset(payload.get("cursor"))
        term = self._directory_term(payload)
        matches = {}
        async for dialog in self.client.iter_dialogs(limit=None):
            if not (getattr(dialog, "is_group", False) or getattr(dialog, "is_channel", False)):
                continue
            try:
                entity = self._group_entity(dialog.entity)
            except SourceAccessError:
                continue
            chat_id = str(utils.get_peer_id(entity))
            username = getattr(entity, "username", None)
            if term and not self._directory_matches(term, dialog.name, chat_id, username, "@" + (username or "")):
                continue
            matches[chat_id] = {"chat_id": chat_id, "title": dialog.name, "username": username,
                                "is_channel": bool(getattr(dialog, "is_channel", False)), "access_state": "joined"}
        # Dialog order changes whenever a new message arrives. Peer-ID ordering
        # keeps pagination stable across those recency changes.
        items = sorted(matches.values(), key=lambda item: int(item["chat_id"]))
        return {"items": items[offset:offset + 50], "next_cursor": str(offset + 50) if len(items) > offset + 50 else None,
                "state": "ready", "q": str(payload.get("q", "")).strip(), "joined_only": True}

    def _directory_term(self, payload):
        term = str(payload.get("q", "")).strip().casefold()
        if len(term) > 160 or any(ord(char) < 32 for char in term):
            raise SourceAccessError("invalid_search_query")
        return term

    def _directory_matches(self, term, *values):
        return any(term in str(value or "").casefold() for value in values)

    def _offset(self, cursor):
        if cursor is None or cursor == "":
            return 0
        if not isinstance(cursor, str) or not re.fullmatch(r"[0-9]{1,7}", cursor):
            raise SourceAccessError("invalid_pagination_cursor")
        value = int(cursor)
        if not 0 <= value <= 1_000_000:
            raise SourceAccessError("invalid_pagination_cursor")
        return value

    async def _admins(self, payload):
        chat_id = str(payload.get("chat_id", ""))
        if not chat_id or len(chat_id) > 160:
            raise SourceAccessError("invalid_source_identifier")
        offset = self._offset(payload.get("cursor"))
        term = self._directory_term(payload)
        entity = await self._entity(chat_id)
        items = []
        if isinstance(entity, Channel):
            if term:
                admins, next_offset = {}, 0
                while True:
                    page, total = await self._channel_admin_page(entity, next_offset, 200)
                    if page and all(user.id in admins for user, _ in page):
                        raise SourceAccessError("directory_pagination_stalled")
                    if not page and total is not None and next_offset < total:
                        raise SourceAccessError("incomplete_admin_page")
                    admins.update({user.id: (user, member) for user, member in page})
                    next_offset += len(page)
                    if not page or (total is not None and next_offset >= total) or (total is None and len(page) < 200):
                        break
                users = list(admins.values())
            else:
                users, _ = await self._channel_admin_page(entity, offset, 51)
        else:
            # Telethon ignores ChannelParticipantsAdmins for a basic Chat.
            # Inspect the actual role, rather than labelling every member admin.
            page = await self.client(GetFullChatRequest(entity.id))
            membership = page.full_chat.participants
            if isinstance(membership, ChatParticipantsForbidden):
                raise SourceAccessError("member_enumeration_restricted")
            users_by_id = {user.id: user for user in page.users}
            roles = [member for member in membership.participants if isinstance(member, (ChatParticipantAdmin, ChatParticipantCreator))]
            if any(member.user_id not in users_by_id for member in roles):
                raise SourceAccessError("incomplete_admin_page")
            admins = [(users_by_id[member.user_id], member) for member in roles]
            users = admins
        if term:
            users = [(user, member) for user, member in users if self._directory_matches(
                term, user.id, _nickname(user, str(user.id)), getattr(user, "username", None), "@" + (getattr(user, "username", None) or ""))]
        if term or isinstance(entity, Chat):
            users = sorted(users, key=lambda entry: entry[0].id)[offset:offset + 51]
        for user, member in users[:50]:
            photo = getattr(user, "photo", None)
            has_photo = bool(photo and not isinstance(photo, UserProfilePhotoEmpty))
            items.append({"sender_id": str(user.id), "nickname": _nickname(user, str(user.id)), "username": getattr(user, "username", None),
                          "admin_kind": "owner" if isinstance(member, (ChatParticipantCreator, ChannelParticipantCreator)) else "administrator",
                          "has_avatar": has_photo, "avatar_state": "not_loaded" if has_photo else "absent"})
        return {"items": items, "next_cursor": str(offset + 50) if len(users)>50 else None, "state": "ready", "manual_id_allowed": True,
                "anonymous_admins_may_be_unlisted": True, "chat_id": str(utils.get_peer_id(entity)), "title": getattr(entity, "title", ""),
                "q": str(payload.get("q", "")).strip()}

    async def _channel_admin_page(self, entity, offset, limit):
        page = await self.client(GetParticipantsRequest(channel=entity, filter=ChannelParticipantsAdmins(), offset=offset, limit=limit, hash=0))
        participants = list(page.participants)
        users_by_id = {user.id: user for user in page.users}
        users = [(users_by_id[member.user_id], member) for member in participants
                 if isinstance(member, (ChannelParticipantAdmin, ChannelParticipantCreator)) and member.user_id in users_by_id]
        if len(users) != len(participants) or len({user.id for user, _ in users}) != len(users):
            raise SourceAccessError("incomplete_admin_page")
        return users, getattr(page, "count", None)
