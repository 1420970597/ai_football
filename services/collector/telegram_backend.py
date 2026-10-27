"""Telegram 采集与远端业务持久层的有限接口。"""

from typing import Any

import httpx


class TelegramBackend:
    def __init__(self, client: httpx.AsyncClient, api_url: str):
        self.client = client
        self.api_url = api_url.rstrip("/")

    async def _call(self, method: str, path: str, body: dict | None = None):
        response = await self.client.request(method, self.api_url + path, json=body)
        response.raise_for_status()
        return response.json()

    async def sources(self) -> list[dict[str, Any]]:
        return (await self._call("GET", "/sources"))["items"]

    async def checkpoint(self, chat_id: str, owner: str) -> dict:
        return await self._call("POST", "/checkpoints/claim", {"chat_id": chat_id, "owner": owner})

    async def advance(self, chat_id: str, owner: str, last_message_id: int, previous: int) -> dict:
        return await self._call("PUT", "/checkpoints", {"chat_id": chat_id, "owner": owner,
                                                       "last_message_id": last_message_id, "previous_message_id": previous})

    async def event(self, body: dict) -> dict:
        return await self._call("POST", "/events", body)

    async def pending(self, limit: int = 30) -> list[dict]:
        return (await self._call("GET", f"/pending?limit={limit}"))["items"]

    async def observed(self, chat_id: str, owner: str, limit: int = 50) -> list[dict]:
        return (await self._call("POST", "/observed", {"chat_id": chat_id, "owner": owner, "limit": limit}))["items"]

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
