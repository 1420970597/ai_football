import asyncio
import unittest
from unittest.mock import AsyncMock

from services.collector.upload_gate import UploadGate
from services.collector.worker import CollectorService


class UploadGateTests(unittest.IsolatedAsyncioTestCase):
    async def queued(self,gate,label,order,*,priority=False):
        async with (gate.priority() if priority else gate):
            order.append(label)
            await asyncio.sleep(0)

    async def test_precise_waits_for_current_then_precedes_normal_fifo(self):
        gate=UploadGate();order=[];await gate.acquire()
        normal=[asyncio.create_task(self.queued(gate,'normal'+str(i),order))for i in range(2)]
        await asyncio.sleep(0)
        precise=asyncio.create_task(self.queued(gate,'precise',order,priority=True));await asyncio.sleep(0)
        self.assertEqual([],order);gate.release()
        await asyncio.gather(*normal,precise)
        self.assertEqual(['precise','normal0','normal1'],order)
        self.assertFalse(gate.locked())

    async def test_continuous_precise_burst_cannot_starve_normal(self):
        gate=UploadGate(priority_burst=2);order=[];await gate.acquire()
        normal=asyncio.create_task(self.queued(gate,'normal',order));await asyncio.sleep(0)
        precise=[asyncio.create_task(self.queued(gate,'precise'+str(i),order,priority=True))for i in range(5)]
        await asyncio.sleep(0);gate.release();await asyncio.gather(normal,*precise)
        self.assertEqual(['precise0','precise1','normal','precise2','precise3','precise4'],order)

    async def test_cancel_waiter_and_cancel_after_handoff_do_not_lose_slot(self):
        gate=UploadGate();await gate.acquire()
        cancelled=asyncio.create_task(gate.acquire(priority=True));await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):await cancelled
        self.assertTrue(gate.locked())
        handed=asyncio.create_task(gate.acquire(priority=True));await asyncio.sleep(0)
        next_waiter=asyncio.create_task(gate.acquire());await asyncio.sleep(0)
        gate.release();handed.cancel()
        with self.assertRaises(asyncio.CancelledError):await handed
        self.assertTrue(await asyncio.wait_for(next_waiter,.2));gate.release()
        self.assertFalse(gate.locked())

    async def test_exception_releases_owner_and_close_cancels_remaining(self):
        gate=UploadGate();order=[]
        with self.assertRaisesRegex(ValueError,'isolated'):
            async with gate.priority():
                waiter=asyncio.create_task(self.queued(gate,'next',order));await asyncio.sleep(0)
                raise ValueError('isolated')
        await asyncio.wait_for(waiter,.2);self.assertEqual(['next'],order)
        await gate.acquire()
        waiters=[asyncio.create_task(gate.acquire(priority=bool(i%2)))for i in range(4)]
        await asyncio.sleep(0);gate.close()
        results=await asyncio.gather(*waiters,return_exceptions=True)
        self.assertTrue(all(isinstance(x,asyncio.CancelledError)for x in results))
        gate.release();self.assertFalse(gate.locked())
        with self.assertRaisesRegex(RuntimeError,'closed'):await gate.acquire()

    async def test_queue_limit_rejects_without_stealing_owner(self):
        gate=UploadGate(max_waiters=1);await gate.acquire()
        waiter=asyncio.create_task(gate.acquire());await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError,'full'):await gate.acquire(priority=True)
        self.assertTrue(gate.locked());gate.release()
        self.assertTrue(await waiter);gate.release()

    async def test_only_fixed_precise_callback_sets_upload_priority(self):
        service=CollectorService.__new__(CollectorService);service.upload=AsyncMock(return_value=1)
        self.assertEqual(1,await service.upload_precise([{'id':'fixture'}]))
        service.upload.assert_awaited_once_with([{'id':'fixture'}],precise=True)
