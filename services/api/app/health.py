"""Short, cached process probes; provider success is a separate fact."""

from __future__ import annotations

import asyncio
import time

import httpx


class HermesProbe:
    def __init__(self, api_url: str, *, ttl: float = 15):
        self.url = api_url.removesuffix("/v1").rstrip("/") + "/health"
        self.ttl = ttl
        self.lock = asyncio.Lock()
        self.expires = 0.0
        self.value = "unavailable"

    async def check(self) -> str:
        if time.monotonic() < self.expires:
            return self.value
        async with self.lock:
            if time.monotonic() < self.expires:
                return self.value
            try:
                async with httpx.AsyncClient(timeout=1.5, trust_env=False, follow_redirects=False) as client:
                    response = await client.get(self.url)
                    self.value = "reachable" if response.status_code == 200 else "unavailable"
            except httpx.HTTPError:
                self.value = "unavailable"
            self.expires = time.monotonic() + self.ttl
            return self.value
