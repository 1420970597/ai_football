from __future__ import annotations

import asyncio
from collections import OrderedDict
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
from .telegram_scheduling import TelegramReadBudget
from .telegram_common import (SourceAccessError, config_int, _message_revision_evidence, _nickname,
                              _trusted_message_author, _utc)

log = logging.getLogger("platform.telegram")
MAX_MEDIA_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_MEDIA_BYTES = 24 * 1024 * 1024
MAX_MEDIA_COUNT = 10
MAX_IMAGE_PIXELS = 40_000_000
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MESSAGE_MEDIA_PREFIX = "v2/telegram/messages"
MESSAGE_MEDIA_RETENTION = "expert_message_evidence"
DIRECTORY_REFRESH_SECONDS = 300
SYNC_HISTORY_SLICE_SECONDS = 2
LIVE_MEDIA_MAX_TASKS = 32
LIVE_MEDIA_CONCURRENCY = 2
SYNC_HISTORY_MAX_MESSAGES = 25
AUTHOR_BACKFILL_PAGE_SIZE = 10
SOURCE_HISTORY_SECONDS = 25
# Stay below the 30-second remote claim lease and the 25-second API read timeout.
RECONCILE_BATCH_SECONDS = 24
RECONCILE_RPC_SECONDS = 5
RECONCILE_EVENT_LIMIT = 3
RECONCILE_COMMIT_CONCURRENCY = 2
# Bound historical event writes across the three concurrently serviced sources.
RECONCILE_GLOBAL_COMMIT_CONCURRENCY = 3
# Recent recovery includes a remote receipt lookup and optional album fetch.
# Keep it below the per-source history slice while allowing their measured tail latency.
RECENT_HISTORY_SECONDS = 15
RECENT_HISTORY_LOOKBACK_SECONDS = 10800
# Keep group entities for several history passes; configuration/session changes clear this cache.
ENTITY_CACHE_SECONDS = 600
ENTITY_CACHE_SIZE = 256
RECENT_HISTORY_PAGE_SIZE = 25
RECENT_HISTORY_RECEIPTS = 2048
RECONCILE_ID_LIMIT = 100
MEDIA_CLEANUP_SECONDS = 5
# Allow bounded API/database tail latency while staying well below the 90s lease.
SOURCE_LEASE_REFRESH_SECONDS = 10
SOURCE_LEASE_REFRESH_SLOW_SECONDS = 1
MEDIA_RETRY_BATCH_SECONDS = 30
MEDIA_RETRY_CONCURRENCY = 2
MEDIA_TRANSFER_SECONDS = 20
MEDIA_COMMIT_SECONDS = 9
SESSION_IDENTITY_SECONDS = 5


def _broadcast_channel(entity) -> bool:
    return (isinstance(entity, Channel) and bool(getattr(entity, 'broadcast', False))
            and not bool(getattr(entity, 'megagroup', False)))


def _factory(session: str, api_id: int, api_hash: str):
    # StringSession 只使用内存，不创建本地 SQLite。
    return TelegramClient(StringSession(session), api_id, api_hash, connection_retries=3,
                          retry_delay=2, request_retries=2, flood_sleep_threshold=0)


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
                 backend=None, client_factory=None, media_store=None, media_delete=None,
                 background_read_interval=2.0):
        self.config = dict(config)
        self.backend = backend or (TelegramBackend(api, api_url) if api is not None and api_url else None)
        self.client_factory = client_factory or _factory
        self.media_store = media_store
        self.media_delete = media_delete
        self.client = None
        self.owner = str(uuid.uuid4())
        self._source_claim_attempted = False
        self.sources: dict[str, dict] = {}
        self.sources_loaded_at = None
        self.source_runtime: dict[str, dict] = {}
        self.source_refresh_lock = asyncio.Lock()
        self.background_reads = TelegramReadBudget(background_read_interval)
        self.recent_next_at: dict[str, float] = {}
        self.archive_next_at: dict[str, float] = {}
        self.reconcile_next_at: dict[str, float] = {}
        self.lease_renew_next_at: dict[str, float] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.live_event_delivery = {
            "received": 0, "staged": 0, "deferred": 0, "unmapped_source": 0,
            "ignored_author": 0,
            "last_received_at": None, "last_staged_at": None,
            "last_deferred_at": None, "last_deferred_category": None,
        }
        self.live_media_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.live_album_tasks: dict[tuple[str,str],asyncio.Task] = {}
        self.media_transfers: set[tuple[str, str]] = set()
        self.media_slots = asyncio.Semaphore(LIVE_MEDIA_CONCURRENCY)
        self.retry_slots = asyncio.Semaphore(MEDIA_RETRY_CONCURRENCY)
        self.session_state_lock = asyncio.Lock()
        self.retry_at: dict[str, float] = {}
        self.directory_retry_at: dict[str, float] = {}
        self.directory_trusted: dict[str, tuple] = {}
        self.reconcile_pending: dict[str, list[dict]] = {}
        self.reconcile_commit_slots = asyncio.Semaphore(RECONCILE_GLOBAL_COMMIT_CONCURRENCY)
        self.recent_recovery: dict[str, dict] = {}
        self.retained_history_floor: dict[str, tuple] = {}
        self.entity_cache: OrderedDict[str, tuple] = OrderedDict()
        self.detection_times: OrderedDict[tuple, str] = OrderedDict()
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
            self.recent_recovery.clear()
            self.retained_history_floor.clear()
            self.entity_cache.clear()
            self.invalid_session_fingerprint = None
            self._verified_identity = None
            self._rejected_identity_binding = None
            self.auth_state = {"state": "idle" if str(config.get("telegram_session", "")).strip() else "disconnected"}
        self.config = dict(config)

    async def disconnect(self):
        self.entity_cache.clear()
        self.retained_history_floor.clear()
        await self._cancel_auth()
        await self._cancel_live_media_tasks()
        if self.client:
            await self.client.disconnect()
            self.client = None
        self._verified_identity = None
        if self.control_server:
            await self.control_server.close()
            self.control_server = None
        if self._source_claim_attempted:
            # A lost claim response may still have granted ownership. Release
            # only after all local writers and Telegram handlers are drained.
            async with asyncio.timeout(4):
                result = await self.backend.release_sources(self.owner)
            count = result.get('released_sources') if isinstance(result, dict) else None
            if type(count) is not int or count < 0:
                raise ValueError('invalid Telegram lease release acknowledgment')
            self._source_claim_attempted = False
            log.info('Telegram source leases released count=%d', count)

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
        client = self.client
        cached = self.entity_cache.get(chat_id)
        if cached and cached[0] is client and time.monotonic() < cached[1]:
            self.entity_cache.move_to_end(chat_id)
            return self._group_entity(cached[2])
        value = int(chat_id) if chat_id.startswith("-") else chat_id
        try:
            entity = await client.get_entity(value)
        except ValueError:
            # StringSession 不持久化实体访问哈希：先从已加入对话解析，不能当成群组无管理员。
            async for dialog in client.iter_dialogs(limit=None):
                entity = dialog.entity
                if str(utils.get_peer_id(entity)) == chat_id or str(getattr(entity, "username", "")).casefold() == chat_id.lstrip("@").casefold():
                    break
            else:
                raise SourceAccessError("account_has_not_joined_or_entity_is_unavailable") from None
        entity = self._group_entity(entity)
        if client is not self.client:
            raise SourceAccessError('session_changed_during_entity_lookup')
        self.entity_cache[chat_id] = (client, time.monotonic() + ENTITY_CACHE_SECONDS, entity)
        self.entity_cache.move_to_end(chat_id)
        while len(self.entity_cache) > ENTITY_CACHE_SIZE:
            self.entity_cache.popitem(last=False)
        return entity

    async def _checkpoint(self, chat_id: str):
        self._source_claim_attempted = True
        checkpoint = await self.backend.checkpoint(chat_id, self.owner)
        self.source_runtime.setdefault(chat_id, {}).update(lease_state='active',
            lease_until=str(checkpoint.get('lease_until') or ''),
            lease_checked_at=datetime.now(timezone.utc).isoformat())
        self.lease_renew_next_at[chat_id] = time.monotonic()+30
        return checkpoint

    async def _cancel_live_media_tasks(self):
        tasks = list(self.live_media_tasks.values())+list(self.live_album_tasks.values())
        self.live_media_tasks.clear()
        self.live_album_tasks.clear()
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
        await self.refresh_sources()
        # Disabled sources are no longer read. Their immutable events remain
        # remote and will be scheduled again when the source is enabled.
        self.reconcile_pending = {chat: rows for chat, rows in self.reconcile_pending.items() if chat in self.sources}
        self.recent_recovery = {chat: state for chat, state in self.recent_recovery.items() if chat in self.sources}
        result = {}
        # 群组之间独立，但控制并发和 Telegram flood；单群失败不阻止其他群。
        gate = asyncio.Semaphore(3)

        async def keep_lease(chat_id):
            started = time.monotonic()
            try:
                async with asyncio.timeout(SOURCE_LEASE_REFRESH_SECONDS):
                    async with self.locks.setdefault(chat_id, asyncio.Lock()):
                        await self._checkpoint(chat_id)
            except Exception as exc:
                result[chat_id]['lease_state'] = 'deferred'
                result[chat_id]['lease_error'] = type(exc).__name__
                log.warning('Telegram source lease refresh deferred category=%s elapsed_ms=%.1f',
                            type(exc).__name__, (time.monotonic() - started) * 1000)
                if result[chat_id]['state'] in {'caught_up', 'catching_up', 'backoff', 'flood_wait'}:
                    result[chat_id]['history_state'] = result[chat_id]['state']
                    result[chat_id]['state'] = 'lease_deferred'
            else:
                elapsed = time.monotonic() - started
                if elapsed >= SOURCE_LEASE_REFRESH_SLOW_SECONDS:
                    log.warning('Telegram source lease refresh slow elapsed_ms=%.1f', elapsed * 1000)

        async def one(chat_id, source):
            async with gate:
                try:
                    async with asyncio.timeout(SOURCE_HISTORY_SECONDS):
                        result[chat_id] = await self._sync_source(chat_id, source)
                    if result[chat_id]["state"] != "backoff":
                        async with asyncio.timeout(SOURCE_HISTORY_SECONDS):
                            result[chat_id]['author_backfill'] = await self._sync_author_backfill(chat_id, source)
                        if time.monotonic() >= self.reconcile_next_at.get(chat_id, 0):
                            reconciliation = await self._reconcile_known(chat_id, source)
                            self.reconcile_next_at[chat_id] = time.monotonic()+30*self.background_reads.interval
                        else:
                            reconciliation = {'state': 'ready', 'scheduled': True}
                        result[chat_id]['reconciliation'] = reconciliation
                        if reconciliation['state'] == 'deferred':
                            result[chat_id]['history_state'] = result[chat_id]['state']
                            result[chat_id]['state'] = 'reconciliation_deferred'
                except FloodWaitError as exc:
                    self.retry_at[chat_id] = time.monotonic() + exc.seconds
                    result[chat_id] = {"state": "flood_wait", "retry_after": exc.seconds}
                    log.warning('Telegram source backoff request=%s retry_seconds=%s',
                                type(getattr(exc, 'request', None)).__name__, exc.seconds)
                except AuthKeyDuplicatedError as exc:
                    await self._invalidate_active_session()
                    result[chat_id] = {"state": "unavailable", "error": type(exc).__name__}
                    log.warning("Telegram session invalidated category=%s", type(exc).__name__)
                except TimeoutError:
                    result[chat_id] = {'state': 'unavailable', 'error': 'TimeoutError', 'reason': 'history_deadline',
                                       'reconciliation_pending': len(self.reconcile_pending.get(chat_id, []))}
                    log.warning('Telegram source deferred category=history_deadline stage=%s',
                                self.source_runtime.get(chat_id, {}).get('sync_stage', 'unknown'))
                except Exception as exc:
                    result[chat_id] = {"state": "unavailable", "error": type(exc).__name__,
                                       'reconciliation_pending': len(self.reconcile_pending.get(chat_id, []))}
                    log.warning("Telegram source unavailable category=%s", type(exc).__name__)
                # Refresh before releasing this slot so the next source wave
                # cannot consume another group's 90-second lease.
                await keep_lease(chat_id)

        await asyncio.gather(*(one(chat_id, source) for chat_id, source in self.sources.items()))
        await self.retry_pending()
        try:
            async with asyncio.timeout(MEDIA_CLEANUP_SECONDS):
                cleanup = await self.cleanup_orphan_media()
        except Exception as exc:
            cleanup = {"state": "deferred", "candidates": 0, "deleted": 0, "error": type(exc).__name__}
            log.warning("Telegram orphan media inventory unavailable category=%s", type(exc).__name__)
        return {"state": "connected", "sources": result, "media_cleanup": cleanup}

    async def refresh_sources(self, *, renew_leases=False):
        """Hot configuration and leases stay independent of historical RPCs."""
        async with self.source_refresh_lock:
            items = await self.backend.sources()
            updated = {str(item['chat_id']): item for item in items}
            for chat, source in updated.items():
                before = self.sources.get(chat)
                if before and before.get('trusted_sender_ids') != source.get('trusted_sender_ids'):
                    self.recent_recovery.pop(chat, None)
                    self.recent_next_at.pop(chat, None)
                    self.archive_next_at.pop(chat, None)
            self.sources = updated
            self.sources_loaded_at = datetime.now(timezone.utc).isoformat()
            self.source_runtime = {chat: row for chat, row in self.source_runtime.items() if chat in updated}
        if renew_leases:
            gate = asyncio.Semaphore(3)
            async def renew(chat):
                async with gate:
                    if time.monotonic() < self.lease_renew_next_at.get(chat, 0):
                        return
                    row = self.source_runtime.setdefault(chat, {})
                    try:
                        async with asyncio.timeout(SOURCE_LEASE_REFRESH_SECONDS):
                            async with self.locks.setdefault(chat, asyncio.Lock()):
                                checkpoint = await self._checkpoint(chat)
                        row.update(lease_state='active', lease_until=str(checkpoint.get('lease_until') or ''),
                                   lease_checked_at=datetime.now(timezone.utc).isoformat())
                    except Exception as exc:
                        row.update(lease_state='unavailable', lease_error=type(exc).__name__)
            await asyncio.gather(*(renew(chat) for chat in self.sources))

    async def _history_messages(self, entity, **options):
        # Every caller requests at most one bounded Telegram page. Background
        # scans share the account budget, including FloodWait across sources.
        async with self.background_reads.slot():
            members = [m async for m in self.client.iter_messages(entity, **options)]
        for message in members:
            yield message

    async def _inspect_window(self, entity, source, payload):
        """Read-only bounded audit, using the same account-wide RPC budget."""
        try:
            start = datetime.fromisoformat(payload['window_start'].replace('Z', '+00:00'))
            end = datetime.fromisoformat(payload['window_end'].replace('Z', '+00:00'))
            if start.utcoffset() is None or end.utcoffset() is None:
                raise ValueError()
            now = datetime.now(timezone.utc)
            if not timedelta(0) < end-start <= timedelta(minutes=30) or end > now:
                raise ValueError()
            if start < now-timedelta(hours=72):
                raise ValueError()
            cursor = payload.get('cursor')
            if cursor is not None and (type(cursor) is not int or cursor <= 0):
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError):
            return {'state': 'invalid_window', 'messages': [], 'complete': False}
        options = {'limit': 100, 'offset_date': end}
        if cursor is not None:
            options['offset_id'] = cursor
        page = [m async for m in self._history_messages(entity, **options)]
        chat = str(utils.get_peer_id(entity))
        items = [{'message_id': int(m.id), 'posted_at': _utc(m.date).isoformat(),
                  'grouped_id': str(m.grouped_id) if getattr(m, 'grouped_id', None) else None,
                  'kind': type(m).__name__, 'has_text': bool(getattr(m, 'message', None)),
                  'has_media': bool(getattr(m, 'media', None)),
                  'trusted': _trusted_message_author(chat, source, m) is not None}
                 for m in page if start <= _utc(m.date) < end]
        complete = len(page) < 100 or any(_utc(m.date) < start for m in page)
        return {'state': 'ready', 'messages': items, 'complete': complete,
                'next_cursor': None if complete else int(page[-1].id),
                'window_start': start.isoformat(), 'window_end': end.isoformat()}

    async def _inspect_media(self,entity,source,identifier):
        """Private, read-only bounded evidence for unsupported-media audits."""
        if type(identifier)is not int or identifier<=0:return {'state':'invalid_message'}
        async with self.background_reads.slot():
            message=await self.client.get_messages(entity,ids=identifier)
        if not message:return {'state':'missing'}
        if _trusted_message_author(str(utils.get_peer_id(entity)),source,message)is None:
            return {'state':'untrusted'}
        posted=_utc(message.date)
        if posted+timedelta(hours=72)<=datetime.now(timezone.utc):return {'state':'expired'}
        file=getattr(message,'file',None);mime=getattr(file,'mime_type',None)
        size=int(getattr(file,'size',0)or 0)
        if mime not in {'video/webm','video/mp4','application/x-tgsticker'}or not 0<size<=MAX_MEDIA_BYTES:
            return {'state':'unsupported_for_visual_audit'}
        body=bytearray()
        async with asyncio.timeout(12):
            async for chunk in self.client.iter_download(message.media,request_size=256*1024):
                body.extend(chunk)
                if len(body)>MAX_MEDIA_BYTES:raise ValueError('audit_media_limit')
        if len(body)!=size:raise ValueError('audit_media_size_mismatch')
        import base64
        return {'state':'ready','posted_at':posted.isoformat(),'mime_type':mime,
                'sha256':hashlib.sha256(body).hexdigest(),'data_base64':base64.b64encode(body).decode()}

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

    async def _retained_history_start(self, chat_id, entity):
        cached = self.retained_history_floor.get(chat_id)
        if cached and cached[0] is self.client and cached[1] > time.monotonic():
            return cached[2]
        cutoff = datetime.now(timezone.utc)-timedelta(hours=72)
        first = None
        # min_id takes priority over offset_date in Telethon. Seek by date
        # separately, then resume the durable ID cursor within that window.
        async for message in self._history_messages(entity, offset_date=cutoff, reverse=True, limit=1):
            first = int(message.id)
            break
        self.retained_history_floor[chat_id] = (self.client, time.monotonic()+300, first)
        return first

    async def _sync_source(self, chat_id: str, source: dict) -> dict:
        if time.monotonic() < max(self.retry_at.get(chat_id, 0), self.background_reads.blocked_until):
            return {"state": "backoff"}
        # Claim the new process's lease before directory writes, but keep the
        # potentially slow Telegram enumeration outside the live-message lock.
        lock = self.locks.setdefault(chat_id, asyncio.Lock())
        runtime = self.source_runtime.setdefault(chat_id, {})
        sync_started = time.monotonic()
        archive_priority = runtime.pop('archive_priority', False)
        runtime['sync_stage'] = 'checkpoint'
        async with lock:
            checkpoint = await self._checkpoint(chat_id)
        if archive_priority:
            # One old-page slice follows a slow recent slice. Its successor
            # returns to the head; archive cannot starve behind slow heads.
            recent = {'state': 'catching_up', 'scheduled': True}
        else:
            runtime['sync_stage'] = 'directory'
            await self._refresh_directory(chat_id, source)
        runtime['sync_stage'] = 'recent_history'
        if not archive_priority and time.monotonic() >= self.recent_next_at.get(chat_id, 0):
            recent = await self._recover_recent(chat_id, source)
            if recent['state'] == 'caught_up':
                self.recent_next_at[chat_id] = time.monotonic()+30*self.background_reads.interval
        elif not archive_priority:
            recent = {'state': 'caught_up', 'scheduled': True}
        original = int(checkpoint["last_message_id"])
        last, count = original, 0
        started = time.monotonic()
        catching_up = False
        if (time.monotonic() < self.archive_next_at.get(chat_id, 0)
                or (original > 0 and recent.get('head_id') == original)):
            return {'state': ('caught_up' if recent['state']=='caught_up' else
                              'catching_up' if recent['state']=='catching_up' else 'recent_deferred'),
                    'last_message_id': original, 'observed': 0, 'recent_history': recent}
        if not archive_priority and time.monotonic()-sync_started >= SOURCE_HISTORY_SECONDS/2:
            runtime['archive_priority'] = True
            return {'state': 'recent_deferred' if recent['state']=='deferred' else 'catching_up',
                    'last_message_id': original, 'observed': 0, 'recent_history': recent,
                    'archive_history': {'state': 'catching_up', 'reason': 'fair_slice_yield'}}
        entity = await self._entity(chat_id)
        runtime['sync_stage'] = 'retention_floor'
        floor = await self._retained_history_start(chat_id, entity)
        if floor is None:
            state = 'recent_deferred' if recent['state']=='deferred' else recent['state']
            return {'state': state, 'last_message_id': original, 'observed': 0, 'recent_history': recent}
        runtime['sync_stage'] = 'archive_page'
        async for message in self._history_messages(entity, min_id=max(original, floor-1), reverse=True,
                                                    limit=SYNC_HISTORY_MAX_MESSAGES):
            if not message or int(message.id) <= last:
                continue
            if _utc(message.date) < datetime.now(timezone.utc)-timedelta(hours=72):
                last, count = int(message.id), count + 1
            else:
                # 先持久原文/待媒体状态，确认后才推进高水位；不使用内存 seen 集合。
                await self._process_message(chat_id, source, message, entity)
                last, count = int(message.id), count + 1
            if count % 25 == 0:
                async with lock:
                    await self.backend.advance(chat_id, self.owner, last, original)
                original = last
            # Release this group's FIFO lock between durable slices so
            # live events do not wait behind years of initial history.
            # The next pass resumes from the persisted high-water mark.
            if count >= SYNC_HISTORY_MAX_MESSAGES or time.monotonic() - started >= SYNC_HISTORY_SLICE_SECONDS:
                catching_up = True
                break
        if last != original:
            async with lock:
                await self.backend.advance(chat_id, self.owner, last, original)
        state = ('recent_deferred' if recent['state'] == 'deferred'
                 else 'catching_up' if catching_up or recent['state'] == 'catching_up' else 'caught_up')
        if state == 'caught_up':
            self.archive_next_at[chat_id] = time.monotonic()+150*self.background_reads.interval
        return {"state": state, "last_message_id": last, "observed": count, 'recent_history': recent}

    async def _recover_recent(self, chat_id: str, source: dict) -> dict:
        """Prioritize recent gaps without advancing the archival checkpoint.

        Receipt cache entries follow durable event commits only. Losing this
        bounded cache on restart causes safe replays, never skipped messages.
        Normally each pass checks the head before older pages. After a slow
        head, one continuation-only slice guarantees old gaps make progress;
        the following slice checks the head again. Live events stay separate.
        """
        if not source.get('trusted_sender_ids'):
            return {'state': 'caught_up', 'scanned': 0, 'persisted': 0}
        state = self.recent_recovery.setdefault(chat_id, {'cursor': None, 'receipts': OrderedDict()})
        receipts = state['receipts']
        # A slow/retried sweep must not age its oldest unresolved gap out of
        # the window before reaching it. Move the boundary only after a
        # complete sweep; restart safely covers the last two hours again.
        cutoff = state.setdefault('cutoff', datetime.now(timezone.utc) - timedelta(seconds=RECENT_HISTORY_LOOKBACK_SECONDS))
        scanned, persisted = 0, 0
        started = time.monotonic()
        phase = 'setup'
        phase_elapsed = {}
        phase_counts = {'pages': 0, 'album_expansions': 0, 'receipt_lookups': 0, 'stage_group_calls': 0}
        # This cache lives for this bounded scan only. Keep independent version
        # fingerprints: Telethon message objects themselves may be mutable.
        albums = {}

        async def timed(name, awaitable):
            nonlocal phase
            phase = name
            phase_started = time.monotonic()
            try:
                return await awaitable
            finally:
                phase_elapsed[name] = phase_elapsed.get(name, 0.0) + (time.monotonic() - phase_started) * 1000

        def timing_fields():
            elapsed = {name: round(value, 1) for name, value in sorted(phase_elapsed.items())}
            return json.dumps({'phase_ms': elapsed, **phase_counts}, sort_keys=True, separators=(',', ':'))

        async def page(entity, cursor, continuation=False):
            nonlocal scanned, persisted
            options = {'max_id': cursor} if cursor is not None else {}
            async def read_page():
                return [m async for m in self._history_messages(entity, limit=RECENT_HISTORY_PAGE_SIZE, **options)]

            phase_counts['pages'] += 1
            members = await timed('telegram_page', read_page())
            if cursor is None and members:
                head = members[0]
                runtime = self.source_runtime.setdefault(chat_id, {})
                runtime.update(last_history_checked_at=datetime.now(timezone.utc).isoformat(),
                    head_message_id=int(head.id), head_posted_at=_utc(head.date).isoformat(),
                    head_author_trusted=_trusted_message_author(chat_id, source, head) is not None,
                    head_kind=type(head).__name__, head_action=type(getattr(head, 'action', None)).__name__,
                    head_has_text=bool(getattr(head, 'message', None)), head_has_media=bool(getattr(head, 'media', None)))
            page_detected_at = datetime.now(timezone.utc).isoformat()
            # Observe before any album I/O; batching must not grant a new
            # detection deadline after a slow Telegram response.
            album_heads = {}
            for message in members:
                if _utc(message.date) < cutoff:
                    break
                if _trusted_message_author(chat_id, source, message) is not None:
                    self._remember_detection(chat_id, [message], page_detected_at)
                    grouped_id = getattr(message, 'grouped_id', None)
                    cached = albums.get(grouped_id)
                    if grouped_id and (cached is None or cached[1].get(message.id) != _message_revision_evidence(message)):
                        album_heads.setdefault(grouped_id, message)
            if len(album_heads) > 1:
                phase_counts['album_expansions'] += len(album_heads)
                expanded = await timed('album_expand', self._expand_page_albums(entity, album_heads))
                for grouped_id, group in expanded.items():
                    albums[grouped_id] = (group, {m.id: _message_revision_evidence(m) for m in group})
            lookup = getattr(self.backend, 'event_receipts', None)
            known = None
            last = cursor
            for message in members:
                if _utc(message.date) < cutoff:
                    return last, True
                scanned += 1
                if _trusted_message_author(chat_id, source, message) is not None:
                    self._remember_detection(chat_id, [message], page_detected_at)
                    grouped_id = getattr(message, 'grouped_id', None)
                    if grouped_id:
                        cached = albums.get(grouped_id)
                        if cached is None or cached[1].get(message.id) != _message_revision_evidence(message):
                            phase_counts['album_expansions'] += 1
                            group = await timed('album_expand', self._expand_album(message, entity))
                            albums[grouped_id] = (group, {m.id: _message_revision_evidence(m) for m in group})
                        else:
                            group = cached[0]
                    else:
                        group = [message]
                    evidence = [_message_revision_evidence(m) for m in group]
                    key = hashlib.sha256(json.dumps([source.get('allow_media', True), evidence],
                                                    sort_keys=True).encode()).hexdigest()
                    if key not in receipts:
                        if known is None:
                            if callable(lookup):
                                phase_counts['receipt_lookups'] += 1
                                known = await timed('receipt_lookup', lookup(
                                    chat_id, self.owner, [int(m.id) for m in members]))
                            else:
                                known = {}
                        phase_counts['stage_group_calls'] += 1
                        result, payload = await timed('stage_group', self._stage_group(
                            chat_id, source, group, known_receipts=known))
                        if result.get('persisted') is not True and result.get('state') != 'ignored_empty':
                            raise RuntimeError('recent Telegram event was not durably committed')
                        if result.get('persisted') is True:
                            receipts[key] = True
                            while len(receipts) > RECENT_HISTORY_RECEIPTS:
                                receipts.popitem(last=False)
                            persisted += 1
                        if payload is not None:
                            self._schedule_live_media(chat_id, payload, group)
                last = int(message.id)
                if continuation:
                    state['cursor'] = last
            return last, len(members) < RECENT_HISTORY_PAGE_SIZE

        try:
            async with asyncio.timeout(RECENT_HISTORY_SECONDS):
                entity = await timed('telegram_entity', self._entity(chat_id))
                source = dict(source, verified_broadcast=_broadcast_channel(entity))
                continuation_only = state.pop('continuation_next', False) and state['cursor'] is not None
                if continuation_only:
                    _, complete = await page(entity, state['cursor'], continuation=True)
                else:
                    head_cursor, complete = await page(entity, None)
                if not complete and not continuation_only:
                    cursor = min(state['cursor'], head_cursor) if state['cursor'] is not None else head_cursor
                    state['cursor'] = cursor
                    # A contended first page has already spent the second
                    # page's fair share. Yield after its durable commits;
                    # preserve the cursor/cutoff and report catching_up.
                    # The next pass still checks new head messages first.
                    if time.monotonic()-started < RECENT_HISTORY_SECONDS/2:
                        _, complete = await page(entity, cursor, continuation=True)
                    else:
                        state['continuation_next'] = True
                if complete:
                    state['cursor'] = None
                    state.pop('cutoff', None)
                result = {'state': 'caught_up' if complete else 'catching_up',
                          'scanned': scanned, 'persisted': persisted,
                          'head_id': self.source_runtime.get(chat_id, {}).get('head_message_id')}
                log.info('Telegram recent history complete state=%s elapsed_ms=%.1f scanned=%s persisted=%s timings=%s',
                         result['state'], (time.monotonic() - started) * 1000, scanned, persisted, timing_fields())
                return result
        except AuthKeyDuplicatedError:
            raise
        except FloodWaitError:
            raise
        except Exception as exc:
            log.warning('Telegram recent history deferred category=%s stage=%s elapsed_ms=%.1f '
                        'scanned=%s persisted=%s timings=%s', type(exc).__name__, phase,
                        (time.monotonic() - started) * 1000, scanned, persisted, timing_fields())
            return {'state': 'deferred', 'scanned': scanned, 'persisted': persisted, 'reason': type(exc).__name__}

    async def _sync_author_backfill(self, chat_id: str, source: dict) -> dict:
        tasks = source.get('author_backfills') or []
        if not tasks:
            return {'state': 'idle', 'processed': 0}
        task = tasks[0]
        sender_id = str(task['sender_id'])
        previous = int(task['cursor'])
        until_id = int(task['until_id'])
        since = _utc(datetime.fromisoformat(str(task['since']).replace('Z', '+00:00')))
        source_peer = sender_id == str(chat_id) and sender_id.startswith('-')
        # Personal-author backfill has the same raw TTL as channel posts.
        # Retention follows the original post, never its latest edit.
        since = max(since, datetime.now(timezone.utc) - timedelta(hours=72))
        if until_id <= 0:
            await self.backend.advance_author_backfill(chat_id, self.owner, sender_id, previous, previous, True)
            return {'state': 'complete', 'sender_id': sender_id, 'processed': 0}
        lock = self.locks.setdefault(chat_id, asyncio.Lock())
        entity = await self._entity(chat_id)
        source = dict(source, verified_broadcast=_broadcast_channel(entity))
        messages = []
        options = {'max_id': previous, 'limit': AUTHOR_BACKFILL_PAGE_SIZE + 1}
        if not source_peer:
            options['from_user'] = int(sender_id)
        # A channel post may have no from_id. An author search would drop it;
        # scan the bounded source history and verify the stable peer below.
        async for message in self._history_messages(entity, **options):
            if int(message.id) <= until_id:
                messages.append(message)
            if len(messages) >= AUTHOR_BACKFILL_PAGE_SIZE + 1:
                break
        page = messages[:AUTHOR_BACKFILL_PAGE_SIZE]
        complete = len(messages) <= AUTHOR_BACKFILL_PAGE_SIZE
        last_cursor = previous
        processed = 0
        for message in page:
            if _utc(message.date) < since:
                complete = True
                break
            if source_peer:
                author = _trusted_message_author(chat_id, source, message)
                if author is None or author[0] != sender_id:
                    last_cursor = int(message.id)
                    continue
            result = await self._process_message(chat_id, source, message, entity)
            if result.get('state') == 'ignored_empty':
                pass
            elif result.get('persisted') is not True:
                raise RuntimeError('author backfill event was not durably committed')
            last_cursor = int(message.id)
            processed += 1
        async with lock:
            await self.backend.advance_author_backfill(
                chat_id, self.owner, sender_id, last_cursor, previous, complete)
        return {'state': 'complete' if complete else 'catching_up', 'sender_id': sender_id, 'processed': processed,
                'cursor': last_cursor, 'until_id': until_id}

    async def _reconcile_known(self, chat_id: str, source: dict):
        # Claims survive process loss remotely. Never acknowledge an event
        # until its checked version/deletion was durably committed.
        pending = self.reconcile_pending.setdefault(chat_id, [])
        now = datetime.now(timezone.utc)
        pending[:] = [item for item in pending if not item.get('claim_lease_until')
                      or _utc(datetime.fromisoformat(item['claim_lease_until'])) > now]
        checked = 0
        stage = 'lease_refresh'
        try:
            async with asyncio.timeout(RECONCILE_BATCH_SECONDS):
                if not pending:
                    stage = 'claim'
                    pending.extend(await self.backend.observed(chat_id, self.owner, limit=RECONCILE_EVENT_LIMIT))
                if not pending:
                    return {'state': 'ready', 'checked': 0, 'pending': 0}
                identifiers = sorted({identifier for item in pending if not item.get('_reconcile_applied')
                                      and not item.get('_reconcile_submission', {}).get('payload')
                                      for identifier in item['content']['member_message_ids']})
                by_id = {}
                if identifiers:
                    async with asyncio.timeout(RECONCILE_RPC_SECONDS):
                        stage = 'telegram_entity'
                        entity = await self._entity(chat_id)
                for offset in range(0, len(identifiers), RECONCILE_ID_LIMIT):
                    stage = 'telegram_budget'
                    async with self.background_reads.slot():
                        async with asyncio.timeout(RECONCILE_RPC_SECONDS):
                            stage = 'telegram_messages'
                            members = await self.client.get_messages(entity, ids=identifiers[offset:offset+RECONCILE_ID_LIMIT])
                    by_id.update({entry.id: entry for entry in members if entry})
                async def commit(item):
                    nonlocal checked
                    async with self.reconcile_commit_slots:
                        reason = await self._commit_reconciliation_item(chat_id, source, item, by_id)
                    if reason != 'event_not_committed':
                        pending.remove(item)
                    if reason is None:
                        checked += 1
                    return reason

                while pending:
                    stage = 'event_commit'
                    tasks = [asyncio.create_task(commit(item))
                             for item in pending[:RECONCILE_COMMIT_CONCURRENCY]]
                    try:
                        reasons = await asyncio.gather(*tasks)
                    finally:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                    reason = next((value for value in reasons if value is not None), None)
                    if reason:
                        return {'state': 'deferred', 'reason': reason,
                                'checked': checked, 'pending': len(pending)}
        except TimeoutError:
            log.warning('Telegram reconciliation deferred category=deadline stage=%s pending=%s',
                        stage, len(pending))
            return {'state': 'deferred', 'reason': 'deadline', 'checked': checked, 'pending': len(pending)}
        return {'state': 'ready', 'checked': checked, 'pending': 0}

    async def _commit_reconciliation_item(self, chat_id: str, source: dict, item: dict, by_id: dict):
        reference = {'event_id': item['id'], 'version_key': item['version_key'], 'claim_id': item['claim_id']}
        result = {}
        try:
            if not item.get('_reconcile_applied'):
                ids = item['content']['member_message_ids']
                submission = item.setdefault('_reconcile_submission', {})
                missing = set(ids) - set(by_id)
                if submission.get('payload'):
                    result = await self.backend.event(submission['payload'])
                elif missing:
                    result = await self._delete(chat_id, sorted(missing), reconciliation=reference,
                                                submission=submission)
                else:
                    result, _ = await self._stage_group(
                        chat_id, source, sorted((by_id[identifier] for identifier in ids), key=lambda entry: entry.id),
                        reconciliation=reference, submission=submission)
                if not result.get('persisted'):
                    return 'event_not_committed'
                item['_reconcile_applied'] = True
            if result.get('reconciliation_acknowledged') is True:
                item['_reconcile_acknowledged'] = True
            if not item.get('_reconcile_acknowledged'):
                await self.backend.acknowledge_observed(chat_id, self.owner, reference)
                item['_reconcile_acknowledged'] = True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:
                raise
            return 'claim_changed'
        return None

    async def _expand_page_albums(self, entity, heads):
        """Use the same complete neighbourhoods, at most 100 IDs per RPC."""
        identifiers = sorted({identifier for message in heads.values()
            for identifier in range(max(1, int(message.id)-20), int(message.id)+21)})
        groups = {grouped_id: {} for grouped_id in heads}
        for offset in range(0, len(identifiers), 100):
            candidates = await self.client.get_messages(entity, ids=identifiers[offset:offset+100])
            for item in candidates:
                grouped_id = getattr(item, 'grouped_id', None) if item else None
                if grouped_id in groups:
                    groups[grouped_id][item.id] = item
        for grouped_id, message in heads.items():
            groups[grouped_id].setdefault(message.id, message)
        return {grouped_id: sorted(group.values(), key=lambda item: item.id)
                for grouped_id, group in groups.items()}

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
        if entity is not None:
            source = dict(source, verified_broadcast=_broadcast_channel(entity))
        if _trusted_message_author(chat_id, source, message) is None:
            return {"state": "ignored_author"}
        self._remember_detection(chat_id, [message])
        members = await self._expand_album(message, entity) if getattr(message, "grouped_id", None) else [message]
        return await self._process_group(chat_id, source, members)

    def _remember_detection(self, chat_id, members, detected_at=None):
        """Include album aggregation/lookup time and preserve the first retry observation.

        The database is authoritative once persisted. This bounded cache also
        preserves an earlier callback when a failed delivery is polled again.
        """
        detected_at = detected_at or datetime.now(timezone.utc).isoformat()
        stamps = []
        newest_edit = max((getattr(m,'edit_date',None) for m in members
                           if getattr(m,'edit_date',None) is not None),default=None)
        for message in members:
            digest = hashlib.sha256(json.dumps(_message_revision_evidence(message)).encode()).hexdigest()
            key = (chat_id, digest)
            self.detection_times.setdefault(key, detected_at)
            # An unchanged album member was detected with the original post;
            # it must not predate the actual edit now forming a new version.
            # Retries of the edited member still preserve its first detection.
            if newest_edit is None or getattr(message,'edit_date',None)==newest_edit:
                stamps.append(self.detection_times[key])
        while len(self.detection_times) > 8192:
            self.detection_times.popitem(last=False)
        return min(stamps, default=detected_at)

    async def _process_group(self, chat_id: str, source: dict, members: list):
        result, _ = await self._stage_group(chat_id, source, members)
        return result

    async def _stage_group(self, chat_id: str, source: dict, members: list, *, reconciliation: dict | None = None,
                           submission: dict | None = None, known_receipts: dict | None = None,
                           detected_at: str | None = None):
        """Persist the complete immutable version before any media transfer."""
        message = members[0]
        author = _trusted_message_author(chat_id, source, message)
        if author is None:
            return {"state": "ignored_author"}, None
        detected_at = self._remember_detection(chat_id, members, detected_at)
        sender_id, channel_post_identity = author
        sender = getattr(message, "sender", None)
        manifests = []
        raw_text = "\n\n".join(item.message for item in members if getattr(item, "message", None))
        reply_member = next((item for item in members if getattr(item, "reply_to_msg_id", None)), None)
        reply = reply_member.reply_to_msg_id if reply_member is not None else None
        reply_context = {}
        sender_task = asyncio.create_task(message.get_sender()) if sender is None and not channel_post_identity else None
        reply_task = None
        if reply:
            try:
                reply_task = asyncio.create_task(reply_member.get_reply_message())
            except BaseException:
                if sender_task is not None:
                    sender_task.cancel()
                    await asyncio.gather(sender_task, return_exceptions=True)
                raise
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
            if isinstance(replied, BaseException):
                raise replied
            if replied is None:
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
            "sender_id": sender_id, "sender_nickname": (
                str(source.get("display_name") or sender_id)[:120]
                if channel_post_identity else _nickname(sender, sender_id)),
            "event_kind": "edit" if any(getattr(item, "edit_date", None) for item in members) else "message",
            "source_version_at": version_at.isoformat(), "posted_at": _utc(message.date).isoformat(),
            "raw_text": raw_text, "reply_to_platform_message_id": str(reply) if reply else None,
            "reply_context": reply_context,
            "media_manifest": manifests, "media_objects": [],
        }
        # 昵称变化不制造消息版本；后台按稳定作者编号独立更新展示名。
        fingerprint = {key: value for key, value in payload.items() if key not in {"owner", "sender_nickname", "media_objects"}}
        payload["version_key"] = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        # Observation metadata is not message content and must not change the
        # immutable version key on retry, media completion, or reconciliation.
        payload['detected_at'] = detected_at
        supported = (len(manifests) <= MAX_MEDIA_COUNT and source.get("allow_media", True)
                     and all(item["mime_type"] in ALLOWED_IMAGE_TYPES and item["bytes"] <= MAX_MEDIA_BYTES
                             and item["width"] * item["height"] <= MAX_IMAGE_PIXELS for item in manifests)
                     and sum(item["bytes"] for item in manifests) <= MAX_MESSAGE_MEDIA_BYTES)
        payload["media_state"] = ("pending" if supported else "unsupported") if manifests else "ready"
        receipt = (known_receipts or {}).get(payload['version_key'])
        if (reconciliation is None and receipt
                and receipt.get('sender_nickname') == payload['sender_nickname']
                and (receipt.get('media_state') == 'ready' and payload['media_state'] in {'ready', 'pending'}
                     or receipt.get('media_state') == payload['media_state'] == 'unsupported')):
            return {'persisted': True, 'state': receipt['media_state'],
                    'message_id': receipt.get('message_id'), 'durable_receipt_reused': True}, None
        if reconciliation is not None:
            payload['reconciliation'] = reconciliation
        if submission is not None:
            submission['payload'] = payload
        first = await self.backend.event(payload)
        if first.get("state") in {"ready", "reconciled", "superseded", "ignored_stale", "deleted", "expired"} or not manifests or not supported:
            return first, None
        payload["media_objects"] = list(first.get("media_objects") or [])
        return first, payload

    async def _complete_pending_media(self, chat_id: str, payload: dict, members: list):
        key = (chat_id, str(payload['version_key']))
        if key in self.media_transfers:
            return {"state": "pending", "duplicate_transfer": True}
        self.media_transfers.add(key)
        try:
            return await self._transfer_pending_media(chat_id, payload, members)
        finally:
            self.media_transfers.discard(key)

    async def _transfer_pending_media(self, chat_id: str, payload: dict, members: list):
        # Transfers never own the group's event/checkpoint lock. The backend
        # rejects obsolete versions and validates the lease again at commit.
        objects = []
        staged = list(payload.get("media_objects") or [])
        image_members = [item for item in members if getattr(item, "media", None)]
        custom_store = self.media_store is not None

        async def cancel_and_collect(tasks):
            for task in tasks:
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            return [value for value in results if not isinstance(value, BaseException)]

        try:
            async with asyncio.timeout(MEDIA_TRANSFER_SECONDS):
                if custom_store:
                    async def store_image(item):
                        async with self.media_slots:
                            return await self.media_store(chat_id, item)

                    tasks = [asyncio.create_task(store_image(item)) for item in image_members]
                    try:
                        objects.extend(await asyncio.gather(*tasks))
                    except BaseException:
                        objects.extend(await cancel_and_collect(tasks))
                        raise
                    if sum(int(item.get('bytes') or 0) for item in objects) > MAX_MESSAGE_MEDIA_BYTES:
                        raise ValueError("message_media_size_limit")
                else:
                    async def prepare_image(item):
                        async with self.media_slots:
                            return await self._prepare_media_candidate(chat_id, item)

                    tasks = [asyncio.create_task(prepare_image(item)) for item in image_members]
                    try:
                        prepared = await asyncio.gather(*tasks)
                    except BaseException:
                        await cancel_and_collect(tasks)
                        raise

                    records = {value.get("object_key"): value for value in staged}
                    for candidate in prepared:
                        record = candidate["record"]
                        previous = records.get(record["object_key"])
                        if previous is not None and previous != record:
                            raise ValueError("media_candidate_evidence_changed")
                        records[record["object_key"]] = record
                    candidate_records = list(records.values())
                    if sum(int(item.get("bytes") or 0) for item in candidate_records) > MAX_MESSAGE_MEDIA_BYTES:
                        raise ValueError("message_media_size_limit")
                    staged = candidate_records

                    previous_keys = {value.get("object_key") for value in payload.get("media_objects") or []}
                    changed = any(value.get("object_key") not in previous_keys for value in staged)
                    if changed:
                        result = await self._commit_media_payload(
                            chat_id, payload | {"media_state": "pending", "media_objects": staged})
                        if result.get("state") not in {"pending", "failed"}:
                            raise SourceAccessError("media_version_no_longer_pending")

                    async def upload_image(candidate):
                        async with self.media_slots:
                            return await self._upload_media_candidate(candidate)

                    tasks = [asyncio.create_task(upload_image(value)) for value in prepared]
                    try:
                        objects.extend(await asyncio.gather(*tasks))
                    except BaseException:
                        objects.extend(await cancel_and_collect(tasks))
                        raise
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
        started = time.monotonic()
        category = 'none'
        try:
            async with asyncio.timeout(MEDIA_COMMIT_SECONDS):
                # The event endpoint atomically validates and renews this owner's
                # lease. A second claim RPC adds latency without stronger evidence.
                # Never hold the group lock while waiting on the remote transaction.
                return await self.backend.event(payload)
        except Exception as exc:
            category = type(exc).__name__
            raise
        finally:
            log.info('Telegram media event commit state=%s images=%s objects=%s category=%s elapsed_ms=%.1f',
                     payload.get('media_state', 'unknown'), len(payload.get('media_manifest') or []),
                     len(payload.get('media_objects') or []), category, (time.monotonic() - started) * 1000)

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
        items = await self.backend.pending(limit=MEDIA_RETRY_CONCURRENCY)
        tasks = [asyncio.create_task(self._retry_pending_item(item)) for item in items]
        if not tasks:
            return
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _retry_pending_item(self, item):
        async with self.retry_slots:
            chat_id = str(item["chat_id"])
            source = self.sources.get(chat_id)
            if source is None or time.monotonic() < self.retry_at.get(chat_id, 0):
                return
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
        chat_id = str(event.chat_id)
        source = self.sources.get(chat_id)
        entity=getattr(event,'chat',None)
        cached=self.entity_cache.get(chat_id)
        if entity is None and cached and cached[0]is self.client and time.monotonic()<cached[1]:
            entity=cached[2]
        if source and entity is not None:
            source=dict(source,verified_broadcast=_broadcast_channel(entity))
        if source and _trusted_message_author(chat_id, source, event.message) is not None:
            self._remember_detection(chat_id, [event.message])
        if getattr(event.message, "grouped_id", None):
            # 新相册由 Album 回调完整聚合；编辑相册仍需读取全组。
            if isinstance(event, events.NewMessage.Event) and not getattr(event.message, "edit_date", None):
                if source and _trusted_message_author(chat_id,source,event.message)is not None:
                    self._schedule_album_fallback(chat_id,event.message,entity)
                return
        await self._live_messages(chat_id, [event.message], entity=entity)

    async def _on_album(self, event):
        key=(str(event.chat_id),str(getattr(event.messages[0],'grouped_id',None)))
        fallback=self.live_album_tasks.pop(key,None)
        if fallback and fallback is not asyncio.current_task():
            fallback.cancel()
            await asyncio.gather(fallback,return_exceptions=True)
        await self._live_messages(str(event.chat_id), list(event.messages), full_album=True,
                                  entity=getattr(event, 'chat', None))

    def _schedule_album_fallback(self,chat_id,message,entity):
        key=(chat_id,str(message.grouped_id))
        if key in self.live_album_tasks or len(self.live_album_tasks)>=32:return
        async def collect():
            await asyncio.sleep(.75)
            async with asyncio.timeout(4):
                peer=entity if entity is not None else await self._entity(chat_id)
                members=await self._expand_album(message,peer)
                await self._live_messages(chat_id,members,full_album=True,entity=peer)
                log.info('Telegram album fallback complete members=%s',len(members))
        task=asyncio.create_task(collect());self.live_album_tasks[key]=task
        def finished(done):
            if self.live_album_tasks.get(key)is done:self.live_album_tasks.pop(key,None)
            try:done.result()
            except asyncio.CancelledError:pass
            except Exception as exc:log.warning('Telegram album fallback deferred category=%s',type(exc).__name__)
        task.add_done_callback(finished)

    async def _live_messages(self, chat_id, members, full_album=False, entity=None):
        delivery = self.live_event_delivery
        delivery["received"] += 1
        detected_at = datetime.now(timezone.utc).isoformat()
        delivery["last_received_at"] = detected_at
        source = self.sources.get(chat_id)
        if not source:
            delivery["unmapped_source"] += 1
            return
        runtime = self.source_runtime.setdefault(chat_id, {})
        runtime['last_live_received_at'] = detected_at
        if entity is not None:
            source = dict(source, verified_broadcast=_broadcast_channel(entity))
        payload = None
        group = sorted(members, key=lambda item: item.id) if full_album else members
        if _trusted_message_author(chat_id, source, group[0]) is None:
            delivery["ignored_author"] += 1
            runtime['ignored_author'] = runtime.get('ignored_author', 0)+1
            return
        detected_at = self._remember_detection(chat_id, group, detected_at)
        started = time.monotonic()
        source_version_at = max(_utc(getattr(item, 'edit_date', None) or item.date) for item in group)
        source_age_ms = max(0.0, (datetime.now(timezone.utc) - source_version_at).total_seconds() * 1000)
        try:
            lookup_started = time.monotonic()
            if not full_album and getattr(members[0], "grouped_id", None):
                if entity is None:
                    entity = await self._entity(chat_id)
                if entity is not None:
                    group = await self._expand_album(members[0], entity)
            lookup_ms = (time.monotonic() - lookup_started) * 1000
            persist_started = time.monotonic()
            # The event transaction validates this owner and lease; a separate checkpoint RPC only serializes live callbacks.
            _, payload = await self._stage_group(chat_id, source, group, detected_at=detected_at)
            persist_ms = (time.monotonic() - persist_started) * 1000
            delivery["staged"] += 1
            runtime['live_staged'] = runtime.get('live_staged', 0)+1
            runtime['last_live_staged_at'] = datetime.now(timezone.utc).isoformat()
            delivery["last_staged_at"] = datetime.now(timezone.utc).isoformat()
            media_ms = 0.0
            if payload is not None:
                media_started = time.monotonic()
                self._schedule_live_media(chat_id, payload, group)
                media_ms = (time.monotonic() - media_started) * 1000
            log.info('Telegram live event persisted source_age_ms=%.1f lookup_ms=%.1f persist_ms=%.1f '
                     'media_ms=%.1f total_ms=%.1f members=%s images=%s',
                     source_age_ms, lookup_ms, persist_ms, media_ms,
                     (time.monotonic() - started) * 1000, len(group),
                     sum(1 for item in group if getattr(item, 'media', None)))
        except Exception as exc:
            delivery["deferred"] += 1
            delivery["last_deferred_at"] = datetime.now(timezone.utc).isoformat()
            delivery["last_deferred_category"] = type(exc).__name__
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

    async def _prepare_media_candidate(self, chat: str, message) -> dict:
        mime = getattr(getattr(message, "file", None), "mime_type", None) or "image/jpeg"
        if mime not in ALLOWED_IMAGE_TYPES:
            raise ValueError("unsupported_media")
        timings = {'download_ms': None, 'validation_ms': None}
        size = 0
        phase = 'download'
        category = 'none'
        try:
            phase_started = time.monotonic()
            chunks = []
            async for chunk in self.client.iter_download(message.media, request_size=256 * 1024):
                size += len(chunk)
                if size > MAX_MEDIA_BYTES:
                    raise ValueError("media_size_limit")
                chunks.append(bytes(chunk))
            body = b"".join(chunks)
            timings['download_ms'] = (time.monotonic() - phase_started) * 1000

            phase = 'validation'
            phase_started = time.monotonic()
            if not body or not _valid_image(body, mime):
                raise ValueError("image_format_mismatch")
            dimensions = await asyncio.to_thread(_image_dimensions, body, mime)
            digest = hashlib.sha256(body).hexdigest()
            timings['validation_ms'] = (time.monotonic() - phase_started) * 1000

            # Keep message media in a dedicated namespace. Avatar and future
            # review-evidence objects must never share this prefix.
            key = f"{MESSAGE_MEDIA_PREFIX}/{chat}/{message.id}/{digest}"
            bucket = os.environ.get("S3_BUCKET", "").strip()
            endpoint = os.environ.get("S3_ENDPOINT_URL", "").strip()
            access = os.environ.get("S3_ACCESS_KEY", "").strip()
            secret = os.environ.get("S3_SECRET_KEY", "")
            if not all((bucket, endpoint, access, secret)):
                raise RuntimeError("remote_S3_not_configured")
            record = {"object_key": key, "mime_type": mime, "sha256": digest, "bytes": size,
                      **dimensions, "purpose": "expert-message", "retention": MESSAGE_MEDIA_RETENTION}
            return {"record": record, "body": body, "mime_type": mime,
                    "bucket": bucket, "endpoint": endpoint, "access": access, "secret": secret}
        except BaseException as exc:
            category = type(exc).__name__
            raise
        finally:
            log.info('Telegram media phases phase=%s category=%s bytes=%s download_ms=%s validation_ms=%s registration_ms=%s upload_ms=%s',
                     phase, category, size,
                     *(f'{timings[name]:.1f}' if timings[name] is not None else 'na'
                       for name in ('download_ms', 'validation_ms')),
                     'na', 'na')

    async def _upload_media_candidate(self, candidate: dict) -> dict:
        record = candidate["record"]
        body = candidate["body"]
        mime = candidate["mime_type"]
        category = 'none'
        upload_ms = None
        try:
            def upload():
                client = boto3.client(
                    "s3", endpoint_url=candidate["endpoint"], region_name=os.environ.get("S3_REGION", "us-east-1"),
                    aws_access_key_id=candidate["access"], aws_secret_access_key=candidate["secret"],
                    use_ssl=os.environ.get("S3_USE_TLS", "true").lower() == "true",
                    config=Config(signature_version="s3v4",
                                  s3={"addressing_style": "path", "payload_signing_enabled": True},
                                  request_checksum_calculation="when_required",
                                  connect_timeout=3, read_timeout=15, retries={"max_attempts": 2},
                                  proxies={"https": os.environ["S3_HTTPS_PROXY"]}
                                  if os.environ.get("S3_HTTPS_PROXY") else None))
                try:
                    client.put_object(
                        Bucket=candidate["bucket"], Key=record["object_key"], Body=body, ContentType=mime,
                        Metadata={"sha256": record["sha256"], "purpose": "expert-message",
                                  "retention": MESSAGE_MEDIA_RETENTION},
                    )
                finally:
                    client.close()

            started = time.monotonic()
            await asyncio.to_thread(upload)
            upload_ms = (time.monotonic() - started) * 1000
            return record
        except BaseException as exc:
            category = type(exc).__name__
            raise
        finally:
            log.info('Telegram media phases phase=upload category=%s bytes=%s download_ms=na validation_ms=na registration_ms=na upload_ms=%s',
                     category, record.get("bytes", 0), f'{upload_ms:.1f}' if upload_ms is not None else 'na')

    async def _store_media(self, chat: str, message, *, stage_object=None) -> dict:
        if self.media_store:
            return await self.media_store(chat, message)
        candidate = await self._prepare_media_candidate(chat, message)
        if stage_object is not None:
            await stage_object(candidate["record"])
        return await self._upload_media_candidate(candidate)

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
                    "live_event_delivery": dict(self.live_event_delivery),
                    "sources_loaded_at": self.sources_loaded_at,
                    "source_runtime": {chat: dict(self.source_runtime.get(chat, {}),
                        trusted_sender_count=len(source.get('trusted_sender_ids') or []))
                        for chat, source in self.sources.items()},
                    "background_reads": self.background_reads.status(),
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
        if path in {"/dialogs", "/admins", "/members", "/source-info"}:
            try:
                if not await self.connect():
                    return {"items": [], "state": "unauthorized", "manual_id_allowed": False,
                            **({'reason': self.auth_state['reason']} if self.auth_state.get('reason') else {})}
                if path == '/source-info':
                    entity = await self._entity(str(payload.get('chat_id', '')))
                    info = {'state': 'ready', 'chat_id': str(utils.get_peer_id(entity)),
                            'source_kind': 'broadcast_channel' if _broadcast_channel(entity) else 'group',
                            'title': str(entity.title)}
                    if 'window_start' in payload or 'window_end' in payload:
                        chat = info['chat_id']
                        source = dict(self.sources.get(chat, {}), verified_broadcast=_broadcast_channel(entity))
                        return info | await self._inspect_window(entity, source, payload)
                    if 'inspect_media_message_id'in payload:
                        chat=info['chat_id']
                        source=dict(self.sources.get(chat,{}),verified_broadcast=_broadcast_channel(entity))
                        return await self._inspect_media(entity,source,payload['inspect_media_message_id'])
                    if payload.get('inspect_messages') is True:
                        chat = info['chat_id']
                        source = dict(self.sources.get(chat, {}), verified_broadcast=_broadcast_channel(entity))
                        info['messages'] = [{'message_id': int(m.id), 'kind': type(m).__name__,
                            'action': type(getattr(m, 'action', None)).__name__, 'posted_at': _utc(m.date).isoformat(),
                            'post': bool(getattr(m, 'post', False)), 'has_text': bool(getattr(m, 'message', None)),
                            'has_media': bool(getattr(m, 'media', None)),
                            'sender_kind': 'missing' if getattr(m, 'sender_id', None) is None else
                                'own_peer' if str(m.sender_id)==chat else 'user' if m.sender_id>0 else 'other_peer',
                            'trusted': _trusted_message_author(chat, source, m) is not None}
                            async for m in self._history_messages(entity, limit=25)]
                    return info
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
                                "is_channel": bool(getattr(dialog, "is_channel", False)), "access_state": "joined",
                                "source_kind": ('broadcast_channel' if _broadcast_channel(entity)
                                                else 'supergroup' if isinstance(entity, Channel) else 'group')}
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
        if _broadcast_channel(entity):
            peer_id = str(utils.get_peer_id(entity))
            nickname = _nickname(entity, peer_id)
            items = [] if offset or (term and not self._directory_matches(
                term, peer_id, nickname, getattr(entity, 'username', None))) else [{
                    'sender_id': peer_id, 'nickname': nickname,
                    'username': getattr(entity, 'username', None), 'admin_kind': 'channel',
                    'sender_identity_kind': 'chat_sender', 'individual_author_resolved': False,
                    'has_avatar': False, 'avatar_state': 'not_loaded'}]
            return {'items': items, 'next_cursor': None, 'state': 'ready', 'manual_id_allowed': True,
                    'chat_id': peer_id, 'title': nickname, 'source_kind': 'broadcast_channel',
                    'trust_automatically_granted': False, 'q': str(payload.get('q', '')).strip()}
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
        if _broadcast_channel(entity):
            return await self._admins(payload)
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
