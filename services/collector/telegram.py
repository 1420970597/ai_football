from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from io import BytesIO
import warnings
import os
import re
import time
from typing import Any
import uuid

import boto3
import httpx
from botocore.config import Config
from telethon import TelegramClient, events, utils
from telethon.errors import (AuthKeyDuplicatedError, FloodWaitError,
                             SessionPasswordNeededError, PasswordHashInvalidError)
from telethon.sessions import StringSession
from PIL import Image
from telethon.tl.types import (Channel, ChannelForbidden, ChannelParticipantsAdmins, ChannelParticipantsSearch,
                              ChannelParticipantAdmin, ChannelParticipantCreator, Chat, ChatForbidden,
                              ChatParticipantAdmin, ChatParticipantCreator, ChatParticipantsForbidden,
                              UserProfilePhotoEmpty)
from telethon.tl.functions.channels import GetParticipantsRequest, GetParticipantRequest
from telethon.tl.functions.messages import GetFullChatRequest

from .telegram_backend import TelegramBackend
from .telegram_control import ControlServer

log = logging.getLogger("platform.telegram")
MAX_MEDIA_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_MEDIA_BYTES = 24 * 1024 * 1024
MAX_MEDIA_COUNT = 10
MAX_IMAGE_PIXELS = 40_000_000
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MESSAGE_MEDIA_PREFIX = "telegram/messages"
MESSAGE_MEDIA_RETENTION = "expert_message_evidence"
DIRECTORY_REFRESH_SECONDS = 300
SYNC_HISTORY_SLICE_SECONDS = 2
LIVE_CHECKPOINT_CACHE_SECONDS = 30
LIVE_MEDIA_MAX_TASKS = 32
LIVE_MEDIA_CONCURRENCY = 2
SYNC_HISTORY_MAX_MESSAGES = 25
SOURCE_HISTORY_SECONDS = 25
RECONCILE_BATCH_SECONDS = 10
RECONCILE_RPC_SECONDS = 3
RECONCILE_EVENT_LIMIT = 10
RECONCILE_ID_LIMIT = 100
MEDIA_CLEANUP_SECONDS = 5
SOURCE_LEASE_REFRESH_SECONDS = 3
MEDIA_RETRY_BATCH_SECONDS = 30
MEDIA_TRANSFER_SECONDS = 20
MEDIA_COMMIT_SECONDS = 5
SESSION_IDENTITY_SECONDS = 5


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


def _image_dimensions(body: bytes, mime: str) -> dict:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(body)) as image:
                width, height = image.size
                frames = getattr(image, 'n_frames', 1)
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError('image_pixel_limit')
                if frames > 100 or width * height * frames > 80_000_000:
                    raise ValueError('image_animation_limit')
                if Image.MIME.get(image.format) != mime:
                    raise ValueError('image_format_mismatch')
                image.verify()
        return {'width': width, 'height': height, 'frames': frames}
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('image_decode_failed') from exc


class TelegramCollector:
    def __init__(self, config: dict[str, str], *, api=None, api_url: str | None = None,
                 backend=None, client_factory=None, media_store=None, media_delete=None):
        self.config = dict(config)
        self.backend = backend or (TelegramBackend(api, api_url) if api is not None and api_url else None)
        self.client_factory = client_factory or _factory
        self.media_store = media_store
        self.media_delete = media_delete
        self.client = None
        self.owner = str(uuid.uuid4())
        self.sources: dict[str, dict] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.live_media_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.media_transfers: set[tuple[str, str]] = set()
        self.media_slots = asyncio.Semaphore(LIVE_MEDIA_CONCURRENCY)
        self.session_state_lock = asyncio.Lock()
        self.retry_at: dict[str, float] = {}
        self.checkpoint_verified_at: dict[str, float] = {}
        self.directory_retry_at: dict[str, float] = {}
        self.directory_trusted: dict[str, tuple] = {}
        self.reconcile_pending: dict[str, list[dict]] = {}
        self.control_server = None
        self.auth_client = None
        self.auth_task = None
        self.auth_state: dict[str, Any] = {"state": "idle"}
        self.auth_deadline = 0.0
        self.account = None
        self.invalid_session_fingerprint = None
        self._verified_identity = None
        self._rejected_identity_binding = None

    @staticmethod
    def _account_id(value):
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
        text = str(value)
        if not re.fullmatch(r'[1-9][0-9]{0,18}', text) or int(text) > 2 ** 63 - 1:
            return None
        return text

    @staticmethod
    def _saved_account(config):
        try:
            account = json.loads(str(config.get("telegram_account", "")))
        except (TypeError, ValueError):
            return None
        if not isinstance(account, dict) or TelegramCollector._account_id(account.get("id")) is None:
            return None
        return {key: account.get(key) for key in ("display_name", "username")} | {"id": str(account["id"])}

    @staticmethod
    def _expected_account_problem(config):
        raw = config.get('telegram_account')
        if raw is None or raw == '':
            return 'session_identity_expected_missing'
        try:
            account = json.loads(str(raw))
        except (TypeError, ValueError):
            return 'session_identity_expected_invalid'
        if isinstance(account, dict) and account.get('id') in (None, ''):
            return 'session_identity_expected_missing'
        return 'session_identity_expected_invalid'

    async def _reject_identity(self, client, reason):
        self._verified_identity = None
        self.account = None
        if self.client is client:
            self.client = None
        self.auth_state = {'state': 'failed', 'reason': reason}
        if client is not None:
            try:
                async with asyncio.timeout(SESSION_IDENTITY_SECONDS):
                    await client.disconnect()
            except Exception:
                log.warning('Telegram identity cleanup deferred')

    @staticmethod
    def _session_fingerprint(session):
        return hashlib.sha256(session.encode("utf-8")).digest()

    async def _mark_session_invalid(self, client, fingerprint):
        await client.disconnect()
        if self.client is client:
            self.client = None
        self.account = None
        self._verified_identity = None
        self.invalid_session_fingerprint = fingerprint
        self.auth_state = {"state": "session_invalid"}

    async def _invalidate_active_session(self):
        async with self.session_state_lock:
            client = self.client
            if client is not None:
                session = str(self.config.get("telegram_session", "")).strip()
                await self._mark_session_invalid(client, self._session_fingerprint(session))

    def _new_client(self, session: str):
        api_id = config_int(self.config.get("telegram_api_id", ""))
        api_hash = str(self.config.get("telegram_api_hash", "")).strip()
        if not api_id or not api_hash:
            raise RuntimeError("Telegram API application is not configured")
        return self.client_factory(session, api_id, api_hash)

    async def connect(self) -> bool:
        async with self.session_state_lock:
            return await self._connect()

    async def _connect(self) -> bool:
        session = str(self.config.get("telegram_session", "")).strip()
        if not session:
            return False
        expected = self._saved_account(self.config)
        if expected is None:
            await self._reject_identity(self.client, self._expected_account_problem(self.config))
            return False
        fingerprint = self._session_fingerprint(session)
        binding = self._session_fingerprint(session + '\0' + expected['id'])
        if self._rejected_identity_binding == binding:
            return False
        if self.invalid_session_fingerprint == fingerprint:
            return False
        candidate = self.client or self._new_client(session)
        adopting = self.client is None
        try:
            if not candidate.is_connected():
                self._verified_identity = None
                await candidate.connect()
            if not await candidate.is_user_authorized():
                await self._mark_session_invalid(candidate, fingerprint)
                return False
            if self._verified_identity != (candidate, binding):
                try:
                    async with asyncio.timeout(SESSION_IDENTITY_SECONDS):
                        actual = await candidate.get_me()
                except TimeoutError:
                    await self._reject_identity(candidate, 'session_identity_timeout')
                    return False
                except AuthKeyDuplicatedError:
                    raise
                except Exception:
                    await self._reject_identity(candidate, 'session_identity_unavailable')
                    return False
                actual_id = self._account_id(getattr(actual, 'id', None))
                if actual_id is None:
                    await self._reject_identity(candidate, 'session_identity_unverifiable')
                    return False
                if actual_id != expected['id']:
                    self._rejected_identity_binding = binding
                    await self._reject_identity(candidate, 'session_identity_mismatch')
                    return False
                self._verified_identity = (candidate, binding)
        except AuthKeyDuplicatedError:
            await self._mark_session_invalid(candidate, fingerprint)
            raise
        except BaseException:
            self._verified_identity = None
            await candidate.disconnect()
            raise
        self.client = candidate
        self.account = expected
        self.auth_state = {"state": "authorized", "account": self.account}
        self.invalid_session_fingerprint = None
        if adopting:
            self._handlers(candidate)
        return True

    def _handlers(self, client):
        client.add_event_handler(self._on_message, events.NewMessage())
        client.add_event_handler(self._on_message, events.MessageEdited())
        client.add_event_handler(self._on_album, events.Album())
        client.add_event_handler(self._on_delete, events.MessageDeleted())

    async def apply_config(self, config: dict[str, str]):
        async with self.session_state_lock:
            await self._apply_config(config)

    async def _apply_config(self, config: dict[str, str]):
        identity = ("telegram_api_id", "telegram_api_hash", "telegram_session")
        changed = any(str(self.config.get(key, "")) != str(config.get(key, "")) for key in identity)
        previous, current = self._saved_account(self.config), self._saved_account(config)
        changed = changed or (previous or {}).get('id') != (current or {}).get('id')
        if changed and self.client:
            await self._cancel_live_media_tasks()
            await self.client.disconnect()
            self.client = None
        if changed:
            self.account = None
            self.directory_retry_at.clear()
            self.directory_trusted.clear()
            self.reconcile_pending.clear()
            self.invalid_session_fingerprint = None
            self._verified_identity = None
            self._rejected_identity_binding = None
            self.auth_state = {"state": "idle" if str(config.get("telegram_session", "")).strip() else "disconnected"}
        self.config = dict(config)

    async def disconnect(self):
        await self._cancel_auth()
        await self._cancel_live_media_tasks()
        if self.client:
            await self.client.disconnect()
            self.client = None
        self._verified_identity = None
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

    async def _checkpoint(self, chat_id: str):
        result = await self.backend.checkpoint(chat_id, self.owner)
        self.checkpoint_verified_at[chat_id] = time.monotonic()
        return result

    async def _ensure_live_checkpoint(self, chat_id: str):
        verified_at = self.checkpoint_verified_at.get(chat_id, 0)
        if time.monotonic() - verified_at >= LIVE_CHECKPOINT_CACHE_SECONDS:
            return await self._checkpoint(chat_id)
        return None

    async def _cancel_live_media_tasks(self):
        tasks = list(self.live_media_tasks.values())
        self.live_media_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule_live_media(self, chat_id: str, payload: dict, members: list):
        key = (chat_id, str(payload['version_key']))
        existing = self.live_media_tasks.get(key)
        if existing and not existing.done():
            return False
        if len(self.live_media_tasks) >= LIVE_MEDIA_MAX_TASKS:
            return False
        task = asyncio.create_task(self._finish_live_media(chat_id, payload, members))
        self.live_media_tasks[key] = task

        def finished(done):
            if self.live_media_tasks.get(key) is done:
                self.live_media_tasks.pop(key, None)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log.warning('Telegram live media deferred category=%s', type(exc).__name__)

        task.add_done_callback(finished)
        return True

    async def _finish_live_media(self, chat_id: str, payload: dict, members: list):
        started = time.monotonic()
        state = 'deferred'
        try:
            result = await self._complete_pending_media(chat_id, payload, members)
            state = result.get('state', 'unknown') if isinstance(result, dict) else 'unknown'
        except FloodWaitError as exc:
            self.retry_at[chat_id] = time.monotonic() + exc.seconds
        except AuthKeyDuplicatedError:
            await self._invalidate_active_session()
        except Exception as exc:
            log.warning('Telegram live media deferred category=%s', type(exc).__name__)
        finally:
            log.info('Telegram live media finished state=%s elapsed_ms=%.1f images=%s',
                     state, (time.monotonic() - started) * 1000,
                     sum(1 for item in members if getattr(item, 'media', None)))

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
            state = self.auth_state.get("state") if self.auth_state.get("state") in {"session_invalid", "failed"} else "unauthorized"
            return {"state": state, "sources": {}, **({'reason': self.auth_state['reason']} if self.auth_state.get('reason') else {})}
        self.sources = {str(item["chat_id"]): item for item in await self.backend.sources()}
        # Disabled sources are no longer read. Their immutable events remain
        # remote and will be scheduled again when the source is enabled.
        self.reconcile_pending = {chat: rows for chat, rows in self.reconcile_pending.items() if chat in self.sources}
        result = {}
        # 群组之间独立，但控制并发和 Telegram flood；单群失败不阻止其他群。
        gate = asyncio.Semaphore(3)

        async def one(chat_id, source):
            async with gate:
                try:
                    async with asyncio.timeout(SOURCE_HISTORY_SECONDS):
                        result[chat_id] = await self._sync_source(chat_id, source)
                    if result[chat_id]["state"] != "backoff":
                        reconciliation = await self._reconcile_known(chat_id, source)
                        result[chat_id]['reconciliation'] = reconciliation
                        if reconciliation['state'] == 'deferred':
                            result[chat_id]['history_state'] = result[chat_id]['state']
                            result[chat_id]['state'] = 'reconciliation_deferred'
                except FloodWaitError as exc:
                    self.retry_at[chat_id] = time.monotonic() + exc.seconds
                    result[chat_id] = {"state": "flood_wait", "retry_after": exc.seconds}
                except AuthKeyDuplicatedError as exc:
                    await self._invalidate_active_session()
                    result[chat_id] = {"state": "unavailable", "error": type(exc).__name__}
                    log.warning("Telegram session invalidated category=%s", type(exc).__name__)
                except TimeoutError:
                    result[chat_id] = {'state': 'unavailable', 'error': 'TimeoutError', 'reason': 'history_deadline',
                                       'reconciliation_pending': len(self.reconcile_pending.get(chat_id, []))}
                    log.warning('Telegram source deferred category=history_deadline')
                except Exception as exc:
                    result[chat_id] = {"state": "unavailable", "error": type(exc).__name__,
                                       'reconciliation_pending': len(self.reconcile_pending.get(chat_id, []))}
                    log.warning("Telegram source unavailable category=%s", type(exc).__name__)

        await asyncio.gather(*(one(chat_id, source) for chat_id, source in self.sources.items()))
        # The second source wave can run while the first has become idle.
        # Refresh before the separate media budget so their 90-second remote
        # leases do not expire during another source's transfer work.
        async def keep_lease(chat_id):
            if result[chat_id]['state'] in {'backoff', 'flood_wait'}:
                return
            try:
                async with gate:
                    async with asyncio.timeout(SOURCE_LEASE_REFRESH_SECONDS):
                        async with self.locks.setdefault(chat_id, asyncio.Lock()):
                            await self._checkpoint(chat_id)
            except Exception as exc:
                result[chat_id]['lease_state'] = 'deferred'
                result[chat_id]['lease_error'] = type(exc).__name__
                if result[chat_id]['state'] in {'caught_up', 'catching_up'}:
                    result[chat_id]['history_state'] = result[chat_id]['state']
                    result[chat_id]['state'] = 'lease_deferred'
        await asyncio.gather(*(keep_lease(chat_id) for chat_id in result))
        await self.retry_pending()
        try:
            async with asyncio.timeout(MEDIA_CLEANUP_SECONDS):
                cleanup = await self.cleanup_orphan_media()
        except Exception as exc:
            cleanup = {"state": "deferred", "candidates": 0, "deleted": 0, "error": type(exc).__name__}
            log.warning("Telegram orphan media inventory unavailable category=%s", type(exc).__name__)
        return {"state": "connected", "sources": result, "media_cleanup": cleanup}

    async def _refresh_directory(self, chat_id: str, source: dict):
        if not callable(getattr(self.backend, 'directory', None)):
            return
        trusted = tuple(sorted(str(value) for value in source.get('trusted_sender_ids', [])))
        now = time.monotonic()
        if now < self.directory_retry_at.get(chat_id, 0) and trusted == self.directory_trusted.get(chat_id):
            return
        self.directory_trusted[chat_id] = trusted
        self.directory_retry_at[chat_id] = now + DIRECTORY_REFRESH_SECONDS
        observed_at = datetime.now(timezone.utc).isoformat()
        admins, profiles, cursor, seen = [], {}, None, set()
        result = {'state': 'unavailable', 'reason': 'directory_unavailable'}
        try:
            async with asyncio.timeout(20):
                while True:
                    page = await self.control('/admins', {'chat_id': chat_id, 'cursor': cursor})
                    result = page
                    if page.get('state') != 'ready':
                        break
                    admins.extend(page['items'])
                    for item in page['items']:
                        profiles[str(item['sender_id'])] = {'sender_id': str(item['sender_id']), 'nickname': item['nickname']}
                    cursor = page.get('next_cursor')
                    if not cursor:
                        break
                    if cursor in seen or len(admins) > 2000:
                        raise SourceAccessError('directory_pagination_stalled')
                    seen.add(cursor)
                for sender_id in trusted:
                    if result.get('state') == 'flood_wait':
                        break
                    if sender_id in profiles:
                        continue
                    try:
                        entity = await self.client.get_entity(int(sender_id))
                        if str(utils.get_peer_id(entity)) == sender_id and not getattr(entity, 'deleted', False):
                            profiles[sender_id] = {'sender_id': sender_id, 'nickname': _nickname(entity, sender_id)}
                    except FloodWaitError:
                        raise
                    except AuthKeyDuplicatedError:
                        raise
                    except Exception:
                        continue
        except AuthKeyDuplicatedError:
            await self._invalidate_active_session()
            raise
        except FloodWaitError as exc:
            result = {'state': 'flood_wait', 'reason': 'flood_wait', 'retry_after': exc.seconds}
        except Exception as exc:
            result = {'state': 'unavailable', 'reason': str(exc) if isinstance(exc, SourceAccessError) else type(exc).__name__}
        retry = max(DIRECTORY_REFRESH_SECONDS, int(result.get('retry_after') or 0))
        self.directory_retry_at[chat_id] = time.monotonic() + retry
        try:
            await self.backend.directory({'chat_id': chat_id, 'owner': self.owner, 'observed_at': observed_at,
                'state': result['state'] if result.get('state') in {'ready', 'flood_wait'} else 'unavailable',
                'reason': result.get('reason'), 'retry_after': result.get('retry_after'),
                'admins': admins if result.get('state') == 'ready' else [],
                'profiles': [item for key, item in profiles.items() if key in trusted]})
        except Exception as exc:
            self.directory_retry_at[chat_id] = time.monotonic() + 30
            log.warning('Telegram directory persistence deferred category=%s', type(exc).__name__)

    async def _sync_source(self, chat_id: str, source: dict) -> dict:
        if time.monotonic() < self.retry_at.get(chat_id, 0):
            return {"state": "backoff"}
        # Directory enumeration can involve several Telegram RPCs; keep it out
        # of the per-chat lock used by new message callbacks.
        await self._refresh_directory(chat_id, source)
        lock = self.locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            checkpoint = await self._checkpoint(chat_id)
            original = int(checkpoint["last_message_id"])
            last, count = original, 0
            started = time.monotonic()
            catching_up = False
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
                # Release this group's FIFO lock between durable slices so
                # live events do not wait behind years of initial history.
                # The next pass resumes from the persisted high-water mark.
                if count >= SYNC_HISTORY_MAX_MESSAGES or time.monotonic() - started >= SYNC_HISTORY_SLICE_SECONDS:
                    catching_up = True
                    break
            if last != original:
                await self.backend.advance(chat_id, self.owner, last, original)
            return {"state": "catching_up" if catching_up else "caught_up", "last_message_id": last, "observed": count}

    async def _reconcile_known(self, chat_id: str, source: dict):
        # Claims survive process loss remotely. Never acknowledge an event
        # until its checked version/deletion was durably committed.
        pending = self.reconcile_pending.setdefault(chat_id, [])
        now = datetime.now(timezone.utc)
        pending[:] = [item for item in pending if not item.get('claim_lease_until')
                      or _utc(datetime.fromisoformat(item['claim_lease_until'])) > now]
        checked = 0
        try:
            async with asyncio.timeout(RECONCILE_BATCH_SECONDS):
                async with self.locks.setdefault(chat_id, asyncio.Lock()):
                    await self._checkpoint(chat_id)
                    if not pending:
                        pending.extend(await self.backend.observed(chat_id, self.owner, limit=RECONCILE_EVENT_LIMIT))
                if not pending:
                    return {'state': 'ready', 'checked': 0, 'pending': 0}
                identifiers = sorted({identifier for item in pending if not item.get('_reconcile_applied')
                                      and not item.get('_reconcile_submission', {}).get('payload')
                                      for identifier in item['content']['member_message_ids']})
                by_id = {}
                if identifiers:
                    async with asyncio.timeout(RECONCILE_RPC_SECONDS):
                        entity = await self._entity(chat_id)
                for offset in range(0, len(identifiers), RECONCILE_ID_LIMIT):
                    async with asyncio.timeout(RECONCILE_RPC_SECONDS):
                        members = await self.client.get_messages(entity, ids=identifiers[offset:offset+RECONCILE_ID_LIMIT])
                    by_id.update({entry.id: entry for entry in members if entry})
                while pending:
                    item = pending[0]
                    reference = {'event_id': item['id'], 'version_key': item['version_key'], 'claim_id': item['claim_id']}
                    ids = item['content']['member_message_ids']
                    members = [by_id[identifier] for identifier in ids if identifier in by_id]
                    async with self.locks.setdefault(chat_id, asyncio.Lock()):
                        await self._checkpoint(chat_id)
                        try:
                            if not item.get('_reconcile_applied'):
                                missing = set(ids) - set(by_id)
                                submission = item.setdefault('_reconcile_submission', {})
                                if submission.get('payload'):
                                    result = await self.backend.event(submission['payload'])
                                elif missing:
                                    result = await self._delete(chat_id, sorted(missing), reconciliation=reference,
                                                                submission=submission)
                                else:
                                    result, _ = await self._stage_group(
                                        chat_id, source, sorted(members, key=lambda entry: entry.id),
                                        reconciliation=reference, submission=submission)
                                if not result.get('persisted'):
                                    return {'state': 'deferred', 'reason': 'event_not_committed',
                                            'checked': checked, 'pending': len(pending)}
                                item['_reconcile_applied'] = True
                            await self.backend.acknowledge_observed(chat_id, self.owner, reference)
                        except httpx.HTTPStatusError as exc:
                            if exc.response.status_code != 409:
                                raise
                            pending.pop(0)
                            return {'state': 'deferred', 'reason': 'claim_changed',
                                    'checked': checked, 'pending': len(pending)}
                        pending.pop(0)
                        checked += 1
                    await asyncio.sleep(0)
        except TimeoutError:
            log.warning('Telegram reconciliation deferred category=deadline pending=%s', len(pending))
            return {'state': 'deferred', 'reason': 'deadline', 'checked': checked, 'pending': len(pending)}
        return {'state': 'ready', 'checked': checked, 'pending': 0}

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
        result, _ = await self._stage_group(chat_id, source, members)
        return result

    async def _stage_group(self, chat_id: str, source: dict, members: list, *, reconciliation: dict | None = None,
                           submission: dict | None = None):
        """Persist the complete immutable version before any media transfer."""
        message = members[0]
        sender_id = str(getattr(message, "sender_id", "") or "")
        if sender_id not in {str(value) for value in source.get("trusted_sender_ids", [])}:
            return {"state": "ignored_author"}, None
        sender = getattr(message, "sender", None)
        manifests = []
        raw_text = "\n\n".join(item.message for item in members if getattr(item, "message", None))
        reply = next((item.reply_to_msg_id for item in members if getattr(item, "reply_to_msg_id", None)), None)
        reply_context = {}
        sender_task = asyncio.create_task(message.get_sender()) if sender is None else None
        reply_task = None
        if reply:
            try:
                reply_task = asyncio.create_task(message.get_reply_message())
            except Exception:
                reply_context = {"unavailable": True}
        lookup_tasks = [task for task in (sender_task, reply_task) if task is not None]
        lookup_results = await asyncio.gather(*lookup_tasks, return_exceptions=True)
        lookup_values = dict(zip((task for task in (sender_task, reply_task) if task is not None), lookup_results))
        if sender_task is not None:
            sender_result = lookup_values[sender_task]
            if isinstance(sender_result, BaseException):
                raise sender_result
            sender = sender_result
        if reply_task is not None:
            replied = lookup_values[reply_task]
            if isinstance(replied, BaseException) or replied is None:
                reply_context = {"unavailable": True}
            else:
                reply_context = {"message_id": str(replied.id), "sender_id": str(getattr(replied, "sender_id", "") or ""),
                                 "text": (getattr(replied, "message", "") or "")[:20000],
                                 "has_media": bool(getattr(replied, "media", None))}
        for item in members:
            if getattr(item, "media", None):
                file = getattr(item, "file", None)
                manifests.append({"message_id": int(item.id), "mime_type": getattr(file, "mime_type", None) or "image/jpeg",
                                  "bytes": int(getattr(file, "size", 0) or 0),
                                  "width": int(getattr(file, "width", 0) or 0), "height": int(getattr(file, "height", 0) or 0)})
        if not raw_text.strip() and not manifests:
            return {"state": "ignored_empty"}, None
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
        if reconciliation is not None:
            payload['reconciliation'] = reconciliation
        if submission is not None:
            submission['payload'] = payload
        first = await self.backend.event(payload)
        if first.get("state") in {"ready", "reconciled", "superseded", "ignored_stale", "deleted"} or not manifests or not supported:
            return first, None
        payload["media_objects"] = list(first.get("media_objects") or [])
        return first, payload

    async def _complete_pending_media(self, chat_id: str, payload: dict, members: list):
        key = (chat_id, str(payload['version_key']))
        if key in self.media_transfers:
            return {"state": "pending", "duplicate_transfer": True}
        self.media_transfers.add(key)
        try:
            async with self.media_slots:
                return await self._transfer_pending_media(chat_id, payload, members)
        finally:
            self.media_transfers.discard(key)

    async def _transfer_pending_media(self, chat_id: str, payload: dict, members: list):
        # Transfers never own the group's event/checkpoint lock. The backend
        # rejects obsolete versions and validates the lease again at commit.
        objects = []
        staged = list(payload.get("media_objects") or [])

        async def stage_object(record):
            if not any(value.get("object_key") == record["object_key"] for value in staged):
                staged.append(record)
            result = await self._commit_media_payload(chat_id, payload | {"media_state": "pending", "media_objects": list(staged)})
            if result.get("state") not in {"pending", "failed"}:
                raise SourceAccessError("media_version_no_longer_pending")

        try:
            async with asyncio.timeout(MEDIA_TRANSFER_SECONDS):
                used = 0
                for item in members:
                    if getattr(item, "media", None):
                        stored = await self._store_media(chat_id, item, stage_object=stage_object)
                        used += int(stored["bytes"])
                        objects.append(stored)
                        if used > MAX_MESSAGE_MEDIA_BYTES:
                            raise ValueError("message_media_size_limit")
        except (FloodWaitError, AuthKeyDuplicatedError):
            raise
        except Exception as exc:
            # Completed objects stay indexed for retry/orphan retention. A
            # cancelled S3 thread may finish remotely, but cannot commit ready.
            candidates = {value["object_key"]: value for value in [*staged, *objects]}
            payload = payload | {"media_state": "failed", "last_error": type(exc).__name__, "media_objects": list(candidates.values())}
        else:
            payload = payload | {"media_state": "ready", "media_objects": objects}
        return await self._commit_media_payload(chat_id, payload)

    async def _commit_media_payload(self, chat_id: str, payload: dict):
        async with asyncio.timeout(MEDIA_COMMIT_SECONDS):
            async with self.locks.setdefault(chat_id, asyncio.Lock()):
                await self._checkpoint(chat_id)
                return await self.backend.event(payload)

    async def retry_pending(self):
        if not self.client or not self.backend:
            return
        try:
            async with asyncio.timeout(MEDIA_RETRY_BATCH_SECONDS):
                await self._retry_pending_batch()
        except TimeoutError:
            # The persisted pending/failed version is retried next pass.
            log.warning("Telegram media retry deferred category=batch_deadline")

    async def _retry_pending_batch(self):
        for item in await self.backend.pending():
            chat_id = str(item["chat_id"])
            source = self.sources.get(chat_id)
            if source is None or time.monotonic() < self.retry_at.get(chat_id, 0):
                continue
            try:
                async with self.locks.setdefault(chat_id, asyncio.Lock()):
                    await self._checkpoint(chat_id)
                entity = await self._entity(chat_id)
                identifiers = item["content"]["member_message_ids"]
                members = [entry for entry in await self.client.get_messages(entity, ids=identifiers) if entry]
                async with self.locks.setdefault(chat_id, asyncio.Lock()):
                    await self._checkpoint(chat_id)
                    missing = set(identifiers) - {entry.id for entry in members}
                    if missing:
                        await self._delete(chat_id, sorted(missing))
                        payload = None
                    else:
                        _, payload = await self._stage_group(chat_id, source, sorted(members, key=lambda entry: entry.id))
                if payload is not None:
                    await self._complete_pending_media(chat_id, payload, members)
            except FloodWaitError as exc:
                self.retry_at[chat_id] = time.monotonic() + exc.seconds
            except AuthKeyDuplicatedError:
                await self._invalidate_active_session()
                raise
            except Exception as exc:
                log.warning("Telegram media retry deferred category=%s", type(exc).__name__)

    async def _on_message(self, event):
        if getattr(event.message, "grouped_id", None):
            # 新相册由 Album 回调完整聚合；编辑相册仍需读取全组。
            if isinstance(event, events.NewMessage.Event) and not getattr(event.message, "edit_date", None):
                return
        await self._live_messages(str(event.chat_id), [event.message], entity=getattr(event, 'chat', None))

    async def _on_album(self, event):
        await self._live_messages(str(event.chat_id), list(event.messages), full_album=True,
                                  entity=getattr(event, 'chat', None))

    async def _live_messages(self, chat_id, members, full_album=False, entity=None):
        source = self.sources.get(chat_id)
        if not source:
            return
        payload = None
        group = sorted(members, key=lambda item: item.id) if full_album else members
        started = time.monotonic()
        lock_started = started
        try:
            async with self.locks.setdefault(chat_id, asyncio.Lock()):
                await self._ensure_live_checkpoint(chat_id)
            lock_wait_ms = (time.monotonic() - lock_started) * 1000
            lookup_started = time.monotonic()
            if not full_album and getattr(members[0], "grouped_id", None):
                if entity is None:
                    entity = await self._entity(chat_id)
                if entity is not None:
                    group = await self._expand_album(members[0], entity)
            lookup_ms = (time.monotonic() - lookup_started) * 1000
            persist_started = time.monotonic()
            _, payload = await self._stage_group(chat_id, source, group)
            persist_ms = (time.monotonic() - persist_started) * 1000
            media_ms = 0.0
            if payload is not None:
                media_started = time.monotonic()
                self._schedule_live_media(chat_id, payload, group)
                media_ms = (time.monotonic() - media_started) * 1000
            log.info('Telegram live event persisted lock_wait_ms=%.1f lookup_ms=%.1f persist_ms=%.1f '
                     'media_ms=%.1f total_ms=%.1f members=%s images=%s',
                     lock_wait_ms, lookup_ms, persist_ms, media_ms,
                     (time.monotonic() - started) * 1000, len(group),
                     sum(1 for item in group if getattr(item, 'media', None)))
        except Exception as exc:
            if isinstance(exc, AuthKeyDuplicatedError):
                await self._invalidate_active_session()
            # 不移动高水位，周期补拉负责重新投递失败的新消息。
            log.warning("Telegram live event deferred category=%s", type(exc).__name__)

    async def _delete(self, chat_id: str, identifiers: list[int], *, reconciliation: dict | None = None,
                      submission: dict | None = None):
        now = datetime.now(timezone.utc).isoformat()
        key = hashlib.sha256(json.dumps(sorted(identifiers)).encode()).hexdigest()
        payload = {"chat_id": chat_id, "owner": self.owner,
            "platform_message_id": "delete:" + key, "member_message_ids": identifiers, "version_key": key,
            "event_kind": "delete", "source_version_at": now, "posted_at": now,
            "media_state": "ready", "raw_text": "", "media_manifest": [], "media_objects": []}
        if reconciliation is not None:
            payload['reconciliation'] = reconciliation
        if submission is not None:
            submission['payload'] = payload
        return await self.backend.event(payload)

    async def _on_delete(self, event):
        chat_id = str(getattr(event, "chat_id", "") or "")
        if chat_id not in self.sources:
            return
        try:
            async with self.locks.setdefault(chat_id, asyncio.Lock()):
                await self._checkpoint(chat_id)
                await self._delete(chat_id, [int(value) for value in event.deleted_ids])
        except Exception as exc:
            if isinstance(exc, AuthKeyDuplicatedError):
                await self._invalidate_active_session()
            log.warning("Telegram deletion unavailable category=%s", type(exc).__name__)

    async def _store_media(self, chat: str, message, *, stage_object=None) -> dict:
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
        dimensions = await asyncio.to_thread(_image_dimensions, body, mime)
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
        record = {"object_key": key, "mime_type": mime, "sha256": digest, "bytes": size,
                  **dimensions, "purpose": "expert-message", "retention": MESSAGE_MEDIA_RETENTION}
        if stage_object is not None:
            # Register the deterministic private object before starting a
            # cancellable thread, so late S3 completion remains reclaimable.
            await stage_object(record)

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
        return record

    async def _delete_media_object(self, object_key: str) -> None:
        """Delete one staged object only after the backend has classified it orphaned."""
        if not object_key or object_key.startswith("/") or ".." in object_key.split("/"):
            raise ValueError("invalid_remote_media_object")
        if self.media_delete:
            await self.media_delete(object_key)
            return
        bucket = os.environ.get("S3_BUCKET", "").strip()
        endpoint = os.environ.get("S3_ENDPOINT_URL", "").strip()
        access = os.environ.get("S3_ACCESS_KEY", "").strip()
        secret = os.environ.get("S3_SECRET_KEY", "")
        if not all((bucket, endpoint, access, secret)):
            raise RuntimeError("remote_S3_not_configured")

        def delete():
            client = boto3.client("s3", endpoint_url=endpoint, region_name=os.environ.get("S3_REGION", "us-east-1"),
                                  aws_access_key_id=access, aws_secret_access_key=secret,
                                  use_ssl=os.environ.get("S3_USE_TLS", "true").lower() == "true",
                                  config=Config(connect_timeout=3, read_timeout=15, retries={"max_attempts": 2},
                                                proxies={"https": os.environ["S3_HTTPS_PROXY"]} if os.environ.get("S3_HTTPS_PROXY") else None))
            try:
                client.delete_object(Bucket=bucket, Key=object_key)
            finally:
                client.close()
        await asyncio.to_thread(delete)

    async def cleanup_orphan_media(self, *, older_than_hours: int = 24, limit: int = 100) -> dict:
        """删除已失败媒体的孤儿对象，并只确认已删除的引用。"""
        if not self.backend or not callable(getattr(self.backend, "media_orphans", None)):
            return {"state": "unsupported", "candidates": 0, "deleted": 0}
        candidates = await self.backend.media_orphans(self.owner, older_than_hours=older_than_hours, limit=limit)
        deleted = []
        for item in candidates:
            key = str(item.get("object_key", ""))
            try:
                await self._delete_media_object(key)
            except Exception as exc:
                log.warning("Telegram orphan media cleanup deferred category=%s", type(exc).__name__)
                continue
            deleted.append(key)
        if deleted:
            await self.backend.acknowledge_media_orphans(self.owner, deleted)
        return {"state": "ready", "candidates": len(candidates), "deleted": len(deleted)}

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
        self._rejected_identity_binding = None
        self._verified_identity = (self.client, self._session_fingerprint(session + '\0' + info['id']))
        self._handlers(self.client)
        self.auth_state = {"state": "authorized", "attempt_id": self.auth_state.get("attempt_id"), "account": info}

    async def control(self, path: str, payload: dict):
        if self.auth_state.get("state") in {"waiting_qr", "waiting_password"} and time.monotonic() >= self.auth_deadline:
            await self._cancel_auth()
            self.auth_state["state"] = "expired"
        if path == "/status":
            health = getattr(self, "source_health", None)
            if health is not None and isinstance(health, dict):
                # The worker intentionally shares its mutable status object
                # with the collector. Never serialize that object directly:
                # it would contain itself under telegram.source_health.
                health = {key: (dict(value) if isinstance(value, dict) else value)
                          for key, value in health.items() if key != "source_health"}
            return {"connected": bool(self.client and self.client.is_connected()),
                    "authorized": bool(self.client and await self.client.is_user_authorized()),
                    "auth": dict(self.auth_state), "account": self.account,
                    "source_health": health}
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
            self._verified_identity = None
            self._rejected_identity_binding = None
            self.auth_state = {"state": "disconnected"}
            return {"state": "disconnected"}
        if path in {"/dialogs", "/admins", "/members"}:
            try:
                if not await self.connect():
                    return {"items": [], "state": "unauthorized", "manual_id_allowed": False,
                            **({'reason': self.auth_state['reason']} if self.auth_state.get('reason') else {})}
                return await ({'/dialogs': self._dialogs, '/admins': self._admins, '/members': self._members}[path](payload))
            except FloodWaitError as exc:
                return {"items": [], "state": "flood_wait", "retry_after": exc.seconds}
            except Exception as exc:
                if isinstance(exc, AuthKeyDuplicatedError):
                    await self._invalidate_active_session()
                    return {"items": [], "state": "unauthorized", "reason": "session_invalid",
                            "manual_id_allowed": False}
                reason = {"ChatAdminRequiredError": "member_enumeration_restricted", "ChannelPrivateError": "source_not_accessible",
                          "UserNotParticipantError": "account_has_not_joined", "ChannelInvalidError": "invalid_source_identifier",
                          "PeerIdInvalidError": "invalid_source_identifier", "UsernameInvalidError": "invalid_source_identifier",
                          "UsernameNotOccupiedError": "invalid_source_identifier"}.get(type(exc).__name__, "upstream_unavailable")
                if isinstance(exc, SourceAccessError):
                    reason = str(exc)
                if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
                    reason = "network_unavailable"
                return {"items": [], "state": "unavailable", "reason": reason,
                        "manual_id_allowed": path in {"/admins", "/members"} and reason == "member_enumeration_restricted",
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

    async def _members(self, payload):
        entity = await self._entity(str(payload.get('chat_id', '')))
        offset, term = self._offset(payload.get('cursor')), self._directory_term(payload)
        users = []
        if isinstance(entity, Chat):
            page = await self.client(GetFullChatRequest(entity.id))
            if isinstance(page.full_chat.participants, ChatParticipantsForbidden):
                raise SourceAccessError('member_enumeration_restricted')
            by_id = {user.id: user for user in page.users}
            for member in page.full_chat.participants.participants:
                user = by_id.get(member.user_id)
                if user is None:
                    raise SourceAccessError('incomplete_member_page')
                if not term or self._directory_matches(term, user.id, _nickname(user, str(user.id)), getattr(user, 'username', None), '@'+(getattr(user, 'username', None) or '')):
                    users.append((user, member))
            users = sorted(users, key=lambda pair: pair[0].id)[offset:offset+51]
        elif (term.isascii() and term.isdigit()) or term.startswith('@'):
            if not offset:
                try:
                    user = await self.client.get_entity(int(term) if term.isdigit() else term)
                    page = await self.client(GetParticipantRequest(entity, user))
                    if type(page.participant).__name__ not in {'ChannelParticipantLeft', 'ChannelParticipantBanned'}:
                        users = [(user, page.participant)]
                except ValueError:
                    raise SourceAccessError('member_identity_unavailable') from None
        else:
            raw_offset, seen = offset, set()
            while len(users)<51:
                page = await self.client(GetParticipantsRequest(entity, ChannelParticipantsSearch(term), raw_offset, min(200, 51-len(users)), 0))
                members, by_id = list(page.participants), {user.id:user for user in page.users}
                if not members:
                    break
                for member in members:
                    if member.user_id in seen or member.user_id not in by_id:
                        raise SourceAccessError('incomplete_member_page')
                    seen.add(member.user_id)
                    users.append((by_id[member.user_id], member))
                raw_offset += len(members)
                if raw_offset>=getattr(page, 'count', raw_offset+1):
                    break
        items=[]
        for user, member in users[:50]:
            if getattr(user, 'deleted', False):
                continue
            role = 'owner' if isinstance(member, (ChatParticipantCreator, ChannelParticipantCreator)) else 'administrator' if isinstance(member, (ChatParticipantAdmin, ChannelParticipantAdmin)) else 'member'
            items.append({'sender_id':str(user.id), 'nickname':_nickname(user,str(user.id)),
                'username':getattr(user,'username',None), 'admin_kind':role, 'is_admin':role!='member',
                'has_avatar':bool(getattr(user,'photo',None)), 'avatar_state':'not_loaded' if getattr(user,'photo',None) else 'absent'})
        return {'state':'ready', 'items':items, 'next_cursor':str(offset+50) if len(users)>50 else None,
                'chat_id':str(utils.get_peer_id(entity)), 'q':str(payload.get('q','')).strip(),
                'member_scope':'all_visible_members', 'manual_id_allowed':True}

    async def _channel_admin_page(self, entity, offset, limit):
        page = await self.client(GetParticipantsRequest(channel=entity, filter=ChannelParticipantsAdmins(), offset=offset, limit=limit, hash=0))
        participants = list(page.participants)
        users_by_id = {user.id: user for user in page.users}
        users = [(users_by_id[member.user_id], member) for member in participants
                 if isinstance(member, (ChannelParticipantAdmin, ChannelParticipantCreator)) and member.user_id in users_by_id]
        if len(users) != len(participants) or len({user.id for user, _ in users}) != len(users):
            raise SourceAccessError("incomplete_admin_page")
        return users, getattr(page, "count", None)
