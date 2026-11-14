"""Account-wide budget for background reads; live delivery never waits here."""
import asyncio
from contextlib import asynccontextmanager
import time

from telethon.errors import FloodWaitError


class TelegramBudgetBusy(Exception):
    """Optional background continuation yields without starting an RPC."""


class TelegramReadBudget:
    def __init__(self, interval=2.0):
        self.interval = interval
        self.next_at = 0.0
        self.blocked_until = 0.0
        self.lock = asyncio.Lock()
        self.requests = 0
        self.flood_waits = 0

    @asynccontextmanager
    async def slot(self):
        async with self.lock:
            delay = self.next_at-time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.requests += 1
            self.next_at = time.monotonic()+self.interval
            try:
                yield
            except FloodWaitError as exc:
                self.flood_waits += 1
                self.blocked_until = time.monotonic()+exc.seconds
                self.next_at = max(self.next_at, self.blocked_until)
                raise

    @asynccontextmanager
    async def optional_slot(self):
        # No suspension between checking and acquiring an available lock.
        # A second page must not wait behind other sources after its first
        # page has already committed; its durable cursor can resume next pass.
        if self.lock.locked() or time.monotonic() < max(self.next_at,self.blocked_until):
            raise TelegramBudgetBusy()
        async with self.slot():
            yield

    def status(self):
        return {'requests': self.requests, 'flood_waits': self.flood_waits,
                'retry_after': round(max(0, self.next_at-time.monotonic()), 1),
                'minimum_interval_seconds': self.interval}
