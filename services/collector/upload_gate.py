"""One upload at a time, with bounded precise priority and cancellation handoff."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager


class UploadGate:
    def __init__(self, max_waiters=32, priority_burst=2):
        self._normal=deque()
        self._priority=deque()
        self._locked=False
        self._closed=False
        self._streak=0
        self._max_waiters=max_waiters
        self._priority_burst=priority_burst

    def locked(self):
        return self._locked

    async def acquire(self, *, priority=False):
        if self._closed:
            raise RuntimeError('upload gate is closed')
        if not self._locked:
            self._locked=True
            self._streak=0
            return True
        if len(self._normal)+len(self._priority)>=self._max_waiters:
            raise RuntimeError('upload queue is full')
        future=asyncio.get_running_loop().create_future()
        queue=self._priority if priority else self._normal
        queue.append(future)
        try:
            await future
            return True
        except BaseException:
            # Cancellation may arrive after release transferred ownership but
            # before this coroutine resumed. Pass that slot on exactly once.
            if future.done() and not future.cancelled():
                self.release()
            else:
                if future in queue:
                    queue.remove(future)
            raise

    def release(self):
        if not self._locked:
            raise RuntimeError('upload gate is not acquired')
        for queue in (self._normal,self._priority):
            while queue and queue[0].done():
                queue.popleft()
        if self._priority and (not self._normal or self._streak<self._priority_burst):
            future=self._priority.popleft()
            self._streak+=1
        elif self._normal:
            future=self._normal.popleft()
            self._streak=0
        else:
            self._locked=False
            self._streak=0
            return
        future.set_result(True)

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self,*args):
        self.release()

    @asynccontextmanager
    async def priority(self):
        await self.acquire(priority=True)
        try:
            yield self
        finally:
            self.release()

    def close(self):
        self._closed=True
        for queue in (self._normal,self._priority):
            while queue:
                future=queue.popleft()
                if not future.done():
                    future.cancel()
