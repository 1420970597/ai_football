"""Exercise real async admission and shutdown without a model or funded order."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app import worker


class ReviewWorkerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, cancel_running):
        started = []
        cancelled = []
        finished = []
        clients = []
        slots_full, fifth_started = asyncio.Event(), asyncio.Event()
        gates = [asyncio.Event() for _ in range(5)]
        listener_stopped = asyncio.Event()
        active = peak = 0
        queued = iter(range(5))
        pool = object()
        client = SimpleNamespace(aclose=AsyncMock())

        async def claim(value):
            self.assertIs(value, pool)
            identity = next(queued, None)
            return None if identity is None else {'id': identity}

        async def process(value, settings, job, *, model_client):
            nonlocal active, peak
            self.assertIs(value, pool)
            identity = job['id']
            clients.append(model_client)
            active += 1
            peak = max(peak, active)
            started.append(identity)
            if len(started) == 4:
                slots_full.set()
            if identity == 4:
                fifth_started.set()
            try:
                await gates[identity].wait()
                finished.append(identity)
            except asyncio.CancelledError:
                cancelled.append(identity)
                raise
            finally:
                active -= 1

        async def listen(url, wake, stop):
            try:
                await stop.wait()
            finally:
                listener_stopped.set()

        close_pool = AsyncMock()
        with patch.object(worker, 'CONCURRENCY', 4), \
             patch.object(worker, 'load_settings', return_value=SimpleNamespace(database_url='isolated')), \
             patch.object(worker.database, 'open_pool', new=AsyncMock(return_value=pool)), \
             patch.object(worker.database, 'close_pool', new=close_pool), \
             patch.object(worker.httpx, 'AsyncClient', return_value=client), \
             patch.object(worker, 'listen_for_jobs', side_effect=listen), \
             patch.object(worker, 'claim_job', side_effect=claim), \
             patch.object(worker, 'process_job', side_effect=process):
            runtime = asyncio.create_task(worker.run())
            try:
                await asyncio.wait_for(slots_full.wait(), 2)
                await asyncio.sleep(0)
                self.assertEqual([0, 1, 2, 3], started)
                self.assertEqual(4, active)
                self.assertFalse(fifth_started.is_set())
                if not cancel_running:
                    gates[1].set()
                    await asyncio.wait_for(fifth_started.wait(), 2)
                    self.assertEqual([1], finished)
                    self.assertEqual([0, 1, 2, 3, 4], started)
                    self.assertEqual(4, active)
            finally:
                runtime.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(runtime, 2)
            self.assertEqual(0, active)
            self.assertEqual(4, peak)
            self.assertTrue(all(value is client for value in clients))
            self.assertTrue(listener_stopped.is_set())
            client.aclose.assert_awaited_once()
            close_pool.assert_awaited_once()
        return started, cancelled, finished

    async def test_four_io_slots_bound_admission_and_refill_one_completed_slot(self):
        started, cancelled, finished = await self.exercise(cancel_running=False)
        self.assertEqual(5, len(started))
        self.assertCountEqual([0, 2, 3, 4], cancelled)
        self.assertEqual([1], finished)

    async def test_shutdown_drains_all_four_jobs_listener_and_shared_http_client(self):
        started, cancelled, finished = await self.exercise(cancel_running=True)
        self.assertCountEqual(started, cancelled)
        self.assertEqual([], finished)
