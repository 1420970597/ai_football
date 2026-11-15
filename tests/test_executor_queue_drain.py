"""Busy work drains without idle sleeps; failure backoff remains bounded."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from services.executor.worker import Executor


class ExecutorQueueDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_orders_drain_serially_without_two_second_gaps(self):
        stop = asyncio.Event()
        executor = Executor(None, 'http://isolated.invalid')
        completed = []

        async def rpc(method, path, **kwargs):
            if path == '/dispatch':
                return {'reserved': 35}
            if len(completed) == 35:
                stop.set()
                return {'kind': 'idle'}
            return {'kind': 'order', 'id': len(completed)}

        async def order(work):
            completed.append(work['id'])

        executor.rpc = rpc
        executor.order_job = order
        with patch('services.executor.worker.asyncio.wait_for', AsyncMock(wraps=asyncio.wait_for)) as waits:
            await executor.run(stop)
        self.assertEqual(list(range(35)), completed)
        self.assertEqual(1, waits.await_count, 'only the final idle cycle may wait')

    async def test_failure_still_waits_before_retrying(self):
        stop = asyncio.Event()
        executor = Executor(None, 'http://isolated.invalid')

        async def rpc(*args, **kwargs):
            stop.set()
            raise RuntimeError('isolated transport failure')

        executor.rpc = rpc
        with patch('services.executor.worker.asyncio.wait_for', AsyncMock(wraps=asyncio.wait_for)) as waits:
            await executor.run(stop)
        waits.assert_awaited_once()
