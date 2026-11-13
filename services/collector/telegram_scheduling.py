"""Account-wide budget for background reads; live delivery never waits here."""
import asyncio
from contextlib import asynccontextmanager
import time

from telethon.errors import FloodWaitError


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

    def status(self):
        return {'requests': self.requests, 'flood_waits': self.flood_waits,
                'retry_after': round(max(0, self.next_at-time.monotonic()), 1),
                'minimum_interval_seconds': self.interval}
