"""Telegram 采集与远端业务持久层的有限接口。"""

import asyncio
from contextlib import asynccontextmanager
import logging
import math
import re
import time
from typing import Any
import uuid

import httpx


log = logging.getLogger('platform.telegram')
_REJECTION_REASONS = {
    'Telegram source lease is not active': 'lease_inactive',
    'Telegram source is owned by another collector': 'lease_owned_elsewhere',
    'checkpoint has changed': 'checkpoint_changed',
    'same message version has conflicting content': 'version_content_conflict',
    'Telegram reconciliation claim has changed': 'reconciliation_claim_changed',
    'Telegram reconciliation retry has conflicting content': 'reconciliation_retry_conflict',
    'Telegram reconciliation claim is no longer active': 'reconciliation_claim_inactive',
    'Telegram reconciliation event scope has changed': 'reconciliation_scope_changed',
    'Telegram reconciliation has not been committed under an active claim': 'reconciliation_uncommitted',
    'sender is not trusted for this source': 'untrusted_author',
    'all message media must be uploaded before review': 'media_incomplete',
    'trusted author mapping is no longer active': 'author_mapping_inactive',
    'author backfill cursor has changed': 'author_cursor_changed',
    'Telegram source is not enabled': 'source_disabled',
}
_REJECTION_CODES = {
    'database_temporarily_unavailable': 'database_temporarily_unavailable',
}
_SOURCE_SERIALIZED_PATHS = frozenset({
    '/directory',
    '/checkpoints/claim',
    '/checkpoints',
    '/author-backfill',
})
_IDEMPOTENT_RETRY_PATHS = frozenset({'/events', '/checkpoints/claim', '/observed'})
_CONNECT_RETRY_PATHS = frozenset({'/events', '/checkpoints/claim'})
_IDEMPOTENT_RETRY_AFTER_FALLBACK_SECONDS = 0.5
_IDEMPOTENT_RETRY_AFTER_MAX_SECONDS = 2.0
_OBSERVED_REQUEST_TIMEOUT = httpx.Timeout(8, connect=3)
_RELEASE_REQUEST_TIMEOUT = httpx.Timeout(3, connect=1)


def _idempotent_retry_delay(response: httpx.Response) -> float:
    value = response.headers.get('Retry-After')
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return _IDEMPOTENT_RETRY_AFTER_FALLBACK_SECONDS
    if not math.isfinite(delay) or delay < 0:
        return _IDEMPOTENT_RETRY_AFTER_FALLBACK_SECONDS
    return min(delay, _IDEMPOTENT_RETRY_AFTER_MAX_SECONDS)


def _checkpoint_outcome_ambiguous(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 503


class TelegramBackend:
    def __init__(self, client: httpx.AsyncClient, api_url: str):
        self.client = client
        self.api_url = api_url.rstrip("/")
        self._source_locks: dict[str, asyncio.Lock] = {}
        self._event_locks: dict[tuple[str, str], tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def _event_lock(self, key: tuple[str, str]):
        entry = self._event_locks.get(key)
        lock, users = entry if entry is not None else (asyncio.Lock(), 0)
        self._event_locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            current_lock, current_users = self._event_locks[key]
            if current_users == 1:
                self._event_locks.pop(key)
            else:
                self._event_locks[key] = (current_lock, current_users - 1)

    async def _call(self, method: str, path: str, body: dict | None = None):
        source_id = str(body.get('chat_id')) if body and body.get('chat_id') is not None else None
        operation = path.split('?', 1)[0]
        message_id = str(body.get('platform_message_id')) if body and body.get('platform_message_id') is not None else None
        started = time.monotonic()
        response_status = 'no_response'
        failure_category = 'none'

        async def request():
            timeout = _OBSERVED_REQUEST_TIMEOUT if operation == '/observed' else None
            if operation == '/checkpoints/release':
                timeout = _RELEASE_REQUEST_TIMEOUT
            kwargs = {'json': body}
            if timeout is not None:
                kwargs['timeout'] = timeout
            for attempt in range(2):
                try:
                    response = await self.client.request(method, self.api_url + path, **kwargs)
                except httpx.TimeoutException as exc:
                    # A failed connection sent no request body. Retry only the
                    # identical event or same-owner claim once under the same
                    # lock and caller deadline. Read ambiguity is not widened.
                    connect_timeout = operation in _CONNECT_RETRY_PATHS and isinstance(exc, httpx.ConnectTimeout)
                    if method != 'POST' or attempt or not (operation == '/observed' or connect_timeout):
                        raise
                    delay = _IDEMPOTENT_RETRY_AFTER_FALLBACK_SECONDS
                    log.warning('Telegram backend retry method=POST operation=%s category=%s attempt=1 retry_after_s=%.3f',
                                operation, 'ConnectTimeout' if connect_timeout else 'timeout', delay)
                else:
                    if not (method == 'POST' and operation in _IDEMPOTENT_RETRY_PATHS
                            and response.status_code == 503 and attempt == 0):
                        return response
                    delay = _idempotent_retry_delay(response)
                    log.warning('Telegram backend retry method=POST operation=%s status=503 attempt=1 retry_after_s=%.3f',
                                operation, delay)
                await asyncio.sleep(delay)
            return response

        try:
            if method == 'POST' and operation == '/events' and source_id and message_id:
                async with self._event_lock((source_id, message_id)):
                    response = await request()
            elif source_id and operation in _SOURCE_SERIALIZED_PATHS:
                lock = self._source_locks.setdefault(source_id, asyncio.Lock())
                async with lock:
                    response = await request()
            else:
                response = await request()
            response_status = str(response.status_code)
            if not response.is_success:
                try:
                    payload = response.json()
                    code = payload.get('code')
                    detail = payload.get('detail')
                except (ValueError, AttributeError):
                    code, detail = None, None
                reason = (_REJECTION_CODES.get(code) if isinstance(code, str) else None)
                reason = reason or (_REJECTION_REASONS.get(detail, 'unclassified') if isinstance(detail, str)
                                    else 'service_unavailable' if response.status_code == 503 else 'unclassified')
                log.warning('Telegram backend rejected method=%s operation=%s status=%s reason=%s',
                            method, operation, response.status_code, reason)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            failure_category = type(exc).__name__
            raise
        finally:
            if operation in {'/events', '/events/receipts'}:
                log.info('Telegram backend request method=%s operation=%s status=%s category=%s elapsed_ms=%.1f',
                         method, operation.lstrip('/'), response_status, failure_category,
                         (time.monotonic() - started) * 1000)

    async def sources(self) -> list[dict[str, Any]]:
        return (await self._call("GET", "/sources"))["items"]

    async def directory(self, body: dict) -> dict:
        return await self._call("PUT", "/directory", body)

    async def checkpoint(self, chat_id: str, owner: str) -> dict:
        return await self._call("POST", "/checkpoints/claim", {"chat_id": chat_id, "owner": owner})

    async def release_sources(self, owner: str) -> dict:
        return await self._call("POST", "/checkpoints/release", {"owner": owner})

    async def advance(self, chat_id: str, owner: str, last_message_id: int, previous: int) -> dict:
        body = {"chat_id": chat_id, "owner": owner, "last_message_id": last_message_id,
                "previous_message_id": previous}

        async def confirm(cause: BaseException) -> dict | None:
            checkpoint = await self.checkpoint(chat_id, owner)
            current = checkpoint.get('last_message_id')
            if type(current) is not int or current < 0:
                raise RuntimeError('invalid Telegram checkpoint confirmation') from cause
            if current >= last_message_id:
                log.info('Telegram checkpoint advance confirmed after ambiguous response')
                return {'last_message_id': current}
            if current != previous:
                raise RuntimeError('Telegram checkpoint changed during ambiguous advance') from cause
            return None

        try:
            return await self._call("PUT", "/checkpoints", body)
        except (httpx.TimeoutException, httpx.HTTPStatusError) as first_error:
            if not _checkpoint_outcome_ambiguous(first_error):
                raise
            confirmed = await confirm(first_error)
            if confirmed is not None:
                return confirmed
            delay = (_idempotent_retry_delay(first_error.response)
                     if isinstance(first_error, httpx.HTTPStatusError) else _IDEMPOTENT_RETRY_AFTER_FALLBACK_SECONDS)
            log.warning('Telegram checkpoint advance retry status=%s attempt=1 retry_after_s=%.3f',
                        first_error.response.status_code if isinstance(first_error, httpx.HTTPStatusError) else 'timeout',
                        delay)
            await asyncio.sleep(delay)

        try:
            return await self._call("PUT", "/checkpoints", body)
        except (httpx.TimeoutException, httpx.HTTPStatusError) as second_error:
            if not _checkpoint_outcome_ambiguous(second_error):
                raise
            confirmed = await confirm(second_error)
            if confirmed is not None:
                return confirmed
            raise

    async def advance_author_backfill(self, chat_id: str, owner: str, sender_id: str,
                                      cursor: int, previous: int, complete: bool) -> dict:
        return await self._call("PUT", "/author-backfill", {
            "chat_id": chat_id, "owner": owner, "sender_id": sender_id,
            "cursor": cursor, "previous_cursor": previous, "complete": complete,
        })

    async def event(self, body: dict) -> dict:
        return await self._call("POST", "/events", body)

    async def event_receipts(self, chat_id: str, owner: str, member_message_ids: list[int]) -> dict[str, dict]:
        response = await self._call('POST', '/events/receipts', {
            'chat_id': chat_id, 'owner': owner, 'member_message_ids': member_message_ids})
        items = response.get('items')
        if not isinstance(items, list) or len(items) > len(member_message_ids):
            raise ValueError('invalid Telegram receipt response')
        if any(not isinstance(item, dict)
               or not re.fullmatch(r'[a-f0-9]{64}', str(item.get('version_key', '')))
               or not isinstance(item.get('sender_nickname'), str) or not item['sender_nickname'].strip()
               or not isinstance(item.get('message_id'), str) or not item['message_id']
               or item.get('media_state') not in {'ready', 'pending', 'failed', 'unsupported'} for item in items):
            raise ValueError('invalid Telegram receipt response')
        return {item['version_key']: item for item in items}

    async def pending(self, limit: int = 30) -> list[dict]:
        return (await self._call("GET", f"/pending?limit={limit}"))["items"]

    async def observed(self, chat_id: str, owner: str, limit: int = 50) -> list[dict]:
        return (await self._call("POST", "/observed", {"chat_id": chat_id, "owner": owner, "limit": limit,
                                                      "reconciliation_protocol": "claim-ack-v1",
                                                      "claim_request_id": str(uuid.uuid4())}))["items"]

    async def acknowledge_observed(self, chat_id: str, owner: str, reconciliation: dict) -> dict:
        return await self._call("POST", "/observed/ack", {"chat_id": chat_id, "owner": owner,
                                                          "reconciliation": reconciliation})

    async def save_session(self, session: str, account: dict) -> dict:
        return await self._call("PUT", "/session", {"session": session, "account": account})

    async def media_orphans(self, owner: str, *, older_than_hours: int = 24, limit: int = 100) -> list[dict]:
        return (await self._call("POST", "/media/orphans", {
            "owner": owner, "older_than_hours": older_than_hours, "limit": limit,
        }))['items']

    async def acknowledge_media_orphans(self, owner: str, object_keys: list[str]) -> dict:
        return await self._call("POST", "/media/orphans/ack", {
            "owner": owner, "object_keys": object_keys,
        })
