import asyncio
import json
from types import SimpleNamespace
import unittest

import httpx

from services.collector.leyu_protocol import LeyuError
from services.collector.upload_gate import UploadGate
from services.collector.worker import CollectorService


class FixtureUploadGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def start(self, gate, keys, *, priority=False, order=None, label=None):
        entered, release = asyncio.Event(), asyncio.Event()

        async def run():
            async with gate.hold(keys, priority=priority):
                if order is not None:
                    order.append(label)
                entered.set()
                await release.wait()

        task = asyncio.create_task(run())
        self.tasks.append(task)
        await asyncio.sleep(0)
        return entered, release, task

    async def test_disjoint_batches_use_two_slots_but_never_three(self):
        gate = UploadGate(max_active=2)
        a, b, c = [await self.start(gate, {key}) for key in 'abc']
        self.assertTrue(a[0].is_set() and b[0].is_set())
        self.assertFalse(c[0].is_set())
        a[1].set()
        await asyncio.wait_for(c[0].wait(), 2)
        b[1].set(); c[1].set()
        await asyncio.gather(*(x[2] for x in (a, b, c)))
        self.assertFalse(gate.locked())

    async def test_intersecting_multi_fixture_batch_is_atomic(self):
        gate = UploadGate(max_active=2)
        a = await self.start(gate, {'a'})
        ab = await self.start(gate, {'a', 'b'})
        b = await self.start(gate, {'b'})
        c = await self.start(gate, {'c'})
        self.assertFalse(ab[0].is_set() or b[0].is_set())
        self.assertTrue(c[0].is_set())
        a[1].set(); c[1].set()
        await asyncio.wait_for(ab[0].wait(), 2)
        self.assertFalse(b[0].is_set())
        ab[1].set()
        await asyncio.wait_for(b[0].wait(), 2)
        b[1].set()
        await asyncio.gather(*(x[2] for x in (a, ab, b, c)))

    async def test_global_barrier_waits_for_both_owners_and_blocks_followers(self):
        gate = UploadGate(max_active=2)
        a = await self.start(gate, {'a'})
        b = await self.start(gate, {'b'})
        barrier = await self.start(gate, None)
        c = await self.start(gate, {'c'})
        a[1].set(); await a[2]
        self.assertFalse(barrier[0].is_set() or c[0].is_set())
        b[1].set(); await asyncio.wait_for(barrier[0].wait(), 2)
        self.assertFalse(c[0].is_set())
        barrier[1].set(); await asyncio.wait_for(c[0].wait(), 2)
        c[1].set()

    async def test_priority_burst_remains_bounded_for_contended_keys(self):
        gate = UploadGate(max_active=2, priority_burst=2)
        order = []
        first = await self.start(gate, {'a'})
        normal = await self.start(gate, {'a'}, order=order, label='normal')
        priority = [await self.start(gate, {'a'}, priority=True, order=order, label=i) for i in range(4)]
        first[1].set()
        for item in (*priority[:2], normal, *priority[2:]):
            await asyncio.wait_for(item[0].wait(), 2)
            item[1].set(); await item[2]
        self.assertEqual([0, 1, 'normal', 2, 3], order)

    async def test_blocked_priority_does_not_waste_disjoint_normal_slot(self):
        gate = UploadGate(max_active=2)
        a = await self.start(gate, {'a'})
        priority = await self.start(gate, {'a'}, priority=True)
        b = await self.start(gate, {'b'})
        self.assertTrue(b[0].is_set())
        self.assertFalse(priority[0].is_set())
        a[1].set(); b[1].set()
        await asyncio.wait_for(priority[0].wait(), 2)
        priority[1].set()

    async def test_cancel_queued_batch_releases_its_ordering_reservation(self):
        gate = UploadGate(max_active=2)
        a = await self.start(gate, {'a'})
        ab = await self.start(gate, {'a', 'b'})
        b = await self.start(gate, {'b'})
        self.assertFalse(b[0].is_set())
        ab[2].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await ab[2]
        await asyncio.wait_for(b[0].wait(), 2)
        a[1].set(); b[1].set()

    async def test_cancel_after_handoff_does_not_leak_or_release_another_owner(self):
        gate = UploadGate(max_active=2)
        await gate.acquire()
        a = await self.start(gate, {'a'})
        b = await self.start(gate, {'b'})
        next_a = await self.start(gate, {'a'})
        gate.release(); a[2].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await a[2]
        await asyncio.wait_for(next_a[0].wait(), 2)
        self.assertTrue(b[0].is_set() and gate.locked())
        next_a[1].set(); b[1].set()
        await asyncio.gather(next_a[2], b[2])
        self.assertFalse(gate.locked())

    async def test_cancel_active_context_and_exception_free_only_its_keys(self):
        gate = UploadGate(max_active=2)
        a = await self.start(gate, {'a'})
        b = await self.start(gate, {'b'})
        next_a = await self.start(gate, {'a'})
        a[2].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await a[2]
        await asyncio.wait_for(next_a[0].wait(), 2)
        next_a[1].set(); await next_a[2]
        with self.assertRaisesRegex(ValueError, 'isolated'):
            async with gate.hold({'a'}):
                raise ValueError('isolated')
        self.assertTrue(gate.locked())
        b[1].set(); await b[2]
        self.assertFalse(gate.locked())

    async def test_queue_limit_and_close_keep_active_owners_valid(self):
        gate = UploadGate(max_active=2, max_waiters=1)
        a = await self.start(gate, {'a'})
        b = await self.start(gate, {'b'})
        queued = await self.start(gate, {'a'})
        with self.assertRaisesRegex(RuntimeError, 'full'):
            async with gate.hold({'b'}):
                self.fail('overflow acquired')
        gate.close()
        with self.assertRaises(asyncio.CancelledError):
            await queued[2]
        self.assertTrue(gate.locked())
        a[1].set(); b[1].set(); await asyncio.gather(a[2], b[2])
        self.assertFalse(gate.locked())
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            async with gate.hold({'c'}):
                self.fail('closed gate acquired')

    async def test_invalid_keys_and_unbounded_capacity_are_rejected(self):
        for capacity in (0, 3, True):
            with self.assertRaises(ValueError):
                UploadGate(max_active=capacity)
        gate = UploadGate(max_active=2)
        for keys in ('abc', set(), {''}, {1}):
            with self.assertRaises(ValueError):
                async with gate.hold(keys):
                    self.fail('invalid keys acquired')

    async def test_mixed_batches_priorities_and_cancellation_do_not_deadlock(self):
        gate = UploadGate(max_active=2)
        active = set(); peak = 0

        async def run(number):
            nonlocal peak
            keys = {str(number % 7), str((number + 1) % 7)}
            async with gate.hold(None if number % 11 == 0 else keys, priority=bool(number % 3)):
                occupied = set().union(*(value for _, value in active)) if active else set()
                self.assertFalse(keys & occupied)
                self.assertLess(len(active), 2)
                if number % 11 == 0:
                    self.assertFalse(active)
                    effective = frozenset(str(i) for i in range(7))
                else:
                    effective = frozenset(keys)
                entry = (number, effective)
                active.add(entry); peak = max(peak, len(active))
                try:
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                finally:
                    active.remove(entry)

        tasks = [asyncio.create_task(run(i)) for i in range(30)]
        self.tasks.extend(tasks)
        await asyncio.sleep(0)
        for task in tasks[3::7]:
            task.cancel()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        self.assertTrue(all(r is None or isinstance(r, asyncio.CancelledError) for r in results))
        self.assertFalse(active or gate.locked())
        self.assertEqual(2, peak)


class FixtureUploadWorkerTests(unittest.IsolatedAsyncioTestCase):
    def service(self, api):
        return CollectorService(api, 'http://api.invalid/internal', 'test-only',
            telegram_factory=lambda *a, **k: SimpleNamespace(),
            runtime_factory=lambda *a, **k: SimpleNamespace(config={}, fixtures={}),
            realtime_factory=lambda *a, **k: SimpleNamespace(stats={}))

    def fixture(self, identity, source=None):
        return {'id': identity, 'source_id': source or identity, 'markets': [], 'markets_complete': True}

    async def test_real_worker_starts_disjoint_uploads_concurrently_with_limit_two(self):
        entered = [asyncio.Event() for _ in range(3)]
        release = asyncio.Event(); active = peak = 0

        async def handler(request):
            nonlocal active, peak
            number = int(json.loads(request.content)['items'][0]['id'])
            active += 1; peak = max(peak, active); entered[number].set()
            try:
                await release.wait()
                return httpx.Response(200, json={'accepted': 1})
            finally:
                active -= 1

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as api:
            service = self.service(api)
            tasks = [asyncio.create_task(service.upload([self.fixture(str(i))])) for i in range(3)]
            try:
                await asyncio.wait_for(asyncio.gather(entered[0].wait(), entered[1].wait()), 2)
                self.assertFalse(entered[2].is_set())
            finally:
                release.set()
                await asyncio.gather(*tasks)
            self.assertEqual(2, peak)
            self.assertFalse(service._upload_lock.locked())

    async def test_shared_id_source_or_missing_identity_stays_serial(self):
        pairs = [(self.fixture('a'), self.fixture('a', 'b')),
                 (self.fixture('a', 'shared'), self.fixture('b', 'shared')),
                 ({'id': 'a', 'markets': []}, self.fixture('b'))]
        for first, second in pairs:
            with self.subTest(pair=pairs.index((first, second))):
                entered = asyncio.Event(); release = asyncio.Event(); order = []
                async def handler(request):
                    order.append(json.loads(request.content)['items'][0]['id'])
                    if len(order) == 1:
                        entered.set(); await release.wait()
                    return httpx.Response(200, json={'accepted': 1})
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as api:
                    service = self.service(api)
                    one = asyncio.create_task(service.upload([first])); await asyncio.wait_for(entered.wait(), 2)
                    two = asyncio.create_task(service.upload([second])); await asyncio.sleep(0)
                    self.assertEqual(1, len(order))
                    release.set(); await asyncio.gather(one, two)
                    self.assertEqual(2, len(order))

    async def test_unknown_http_and_invalid_receipt_never_restore_delivery_evidence(self):
        fixture = self.fixture('a')
        for failure in ('unknown', 'invalid'):
            async def handler(request):
                if failure == 'unknown':
                    raise httpx.ReadTimeout('isolated', request=request)
                return httpx.Response(200, json={'accepted': True})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as api:
                service = self.service(api); service._delivered_snapshots.remember([fixture])
                self.assertTrue(service._delivered_snapshots.matches(fixture))
                with self.assertRaises((httpx.ReadTimeout, LeyuError)):
                    await service.upload([fixture])
                self.assertFalse(service._delivered_snapshots.matches(fixture))
                self.assertFalse(service._upload_lock.locked())

    async def test_failed_disjoint_request_does_not_retire_successful_sibling_evidence(self):
        first = asyncio.Event(); second = asyncio.Event()
        async def handler(request):
            identity = json.loads(request.content)['items'][0]['id']
            if identity == 'a':
                first.set(); await second.wait()
                raise httpx.ReadTimeout('isolated', request=request)
            second.set(); await first.wait()
            return httpx.Response(200, json={'accepted': 1})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as api:
            service = self.service(api)
            a, b = self.fixture('a'), self.fixture('b')
            service._delivered_snapshots.remember([a, b])
            result = await asyncio.wait_for(asyncio.gather(service.upload([a]), service.upload([b]),
                return_exceptions=True), 3)
            self.assertIsInstance(result[0], httpx.ReadTimeout)
            self.assertEqual(1, result[1])
            self.assertFalse(service._delivered_snapshots.matches(a))
            self.assertTrue(service._delivered_snapshots.matches(b))
            self.assertFalse(service._upload_lock.locked())

    async def test_rejected_http_receipt_is_distinct_from_transport_failure(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(503, json={'detail': 'isolated'}))) as api:
            service = self.service(api)
            with self.assertLogs('platform.collector', level='INFO') as logs:
                with self.assertRaises(httpx.HTTPStatusError):
                    await service.upload([self.fixture('a')])
            self.assertIn('outcome=response_error', logs.output[0])
            self.assertIn('response_status=503', logs.output[0])
            self.assertFalse(service._upload_lock.locked())
