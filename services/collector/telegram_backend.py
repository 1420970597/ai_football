"""Telegram 采集与远端业务持久层的有限接口。"""

import asyncio
from contextlib import asynccontextmanager
import logging
import re
from typing import Any

import httpx


log = logging.getLogger('platform.telegram')
_REJECTION_REASONS = {
    'Telegram source lease is not active': 'lease_inactive',
    'Telegram source is owned by another collector': 'lease_owned_elsewhere',
    'checkpoint has changed': 'checkpoint_changed',
    'same message version has conflicting content': 'version_content_conflict',
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
_IDEMPOTENT_RETRY_PATHS = frozenset({'/events', '/checkpoints/claim'})


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

        async def request():
            response = await self.client.request(method, self.api_url + path, json=body)
            if method == 'POST' and operation in _IDEMPOTENT_RETRY_PATHS and response.status_code == 503:
                log.warning('Telegram backend retry method=POST operation=%s status=503 attempt=1', operation)
                await asyncio.sleep(0.1)
                response = await self.client.request(method, self.api_url + path, json=body)
            return response

        if method == 'POST' and operation == '/events' and source_id and message_id:
            async with self._event_lock((source_id, message_id)):
                response = await request()
        elif source_id and operation in _SOURCE_SERIALIZED_PATHS:
            lock = self._source_locks.setdefault(source_id, asyncio.Lock())
            async with lock:
                response = await request()
        else:
            response = await request()
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
                        method, path.split('?', 1)[0], response.status_code, reason)
        response.raise_for_status()
        return response.json()

    async def sources(self) -> list[dict[str, Any]]:
        return (await self._call("GET", "/sources"))["items"]

    async def directory(self, body: dict) -> dict:
        return await self._call("PUT", "/directory", body)

    async def checkpoint(self, chat_id: str, owner: str) -> dict:
        return await self._call("POST", "/checkpoints/claim", {"chat_id": chat_id, "owner": owner})

    async def advance(self, chat_id: str, owner: str, last_message_id: int, previous: int) -> dict:
        return await self._call("PUT", "/checkpoints", {"chat_id": chat_id, "owner": owner,
                                                       "last_message_id": last_message_id, "previous_message_id": previous})

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
                                                      "reconciliation_protocol": "claim-ack-v1"}))["items"]

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
