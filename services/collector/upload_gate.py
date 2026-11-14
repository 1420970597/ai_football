"""Bounded atomic fixture leases, precise priority and cancellation handoff."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass(eq=False)
class _Lease:
    future: asyncio.Future
    keys: frozenset[str] | None
    order: int


class UploadGate:
    def __init__(self, max_waiters=32, priority_burst=2, *, max_active=1):
        if type(max_active) is not int or max_active not in (1, 2):
            raise ValueError('upload capacity must be one or two')
        if type(max_waiters) is not int or max_waiters < 0 or type(priority_burst) is not int or priority_burst < 1:
            raise ValueError('invalid upload queue budget')
        self._normal=deque()
        self._priority=deque()
        self._active: set[_Lease] = set()
        self._exclusive: _Lease | None = None
        self._sequence=0
        self._closed=False
        self._streak=0
        self._max_waiters=max_waiters
        self._priority_burst=priority_burst
        self._max_active=max_active

    def locked(self):
        return bool(self._active)

    async def acquire(self, *, priority=False):
        self._exclusive=await self._acquire(None, priority=priority)
        return True

    async def _acquire(self, keys, *, priority=False):
        if self._closed:
            raise RuntimeError('upload gate is closed')
        if keys is not None:
            if isinstance(keys, str):
                raise ValueError('fixture keys must be a nonempty collection')
            keys=frozenset(keys)
            if not keys or any(not isinstance(key,str) or not key for key in keys):
                raise ValueError('fixture keys must be nonempty strings')
        future=asyncio.get_running_loop().create_future()
        self._sequence+=1
        lease=_Lease(future,keys,self._sequence)
        queue=self._priority if priority else self._normal
        queue.append(lease)
        self._drain()
        if lease not in self._active and len(self._normal)+len(self._priority)>self._max_waiters:
            queue.remove(lease)
            future.cancel()
            raise RuntimeError('upload queue is full')
        try:
            await future
            return lease
        except BaseException:
            # A grant may already own keys before its coroutine resumes.
            if lease in self._active:
                self._release(lease)
            else:
                if lease in queue:
                    queue.remove(lease)
                self._drain()
            raise

    def release(self):
        if self._exclusive is None:
            raise RuntimeError('upload gate is not acquired')
        lease,self._exclusive=self._exclusive,None
        self._release(lease)

    def _release(self, lease):
        self._active.remove(lease)
        self._drain()

    def _eligible(self, queue):
        earlier_keys=set()
        for lease in queue:
            keys=lease.keys
            if keys is None:
                if not self._active and not earlier_keys:
                    return lease
                # A global barrier cannot be bypassed by this queue.
                return None
            earlier_barrier=any(other.keys is None and other.order<lease.order
                for pending in (self._normal,self._priority) for other in pending)
            conflict=any(owner.keys is None or keys & owner.keys for owner in self._active)
            if not earlier_barrier and not conflict and not (keys & earlier_keys):
                return lease
            earlier_keys.update(keys)
        return None

    def _drain(self):
        for queue in (self._normal,self._priority):
            for lease in tuple(queue):
                if lease.future.done():
                    queue.remove(lease)
        while not self._closed and len(self._active)<self._max_active:
            priority=self._eligible(self._priority)
            normal=self._eligible(self._normal)
            if priority is not None and (normal is None or self._streak<self._priority_burst):
                lease,queue=priority,self._priority
                self._streak+=1
            elif normal is not None:
                lease,queue=normal,self._normal
                self._streak=0
            else:
                break
            queue.remove(lease)
            self._active.add(lease)
            lease.future.set_result(True)
        if not self._active and not self._normal and not self._priority:
            self._streak=0

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self,*args):
        self.release()

    @asynccontextmanager
    async def priority(self):
        async with self.hold(None, priority=True):
            yield self

    @asynccontextmanager
    async def hold(self, keys, *, priority=False):
        lease=await self._acquire(keys, priority=priority)
        try:
            yield self
        finally:
            self._release(lease)

    def close(self):
        self._closed=True
        for queue in (self._normal,self._priority):
            while queue:
                future=queue.popleft().future
                if not future.done():
                    future.cancel()
