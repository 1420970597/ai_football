import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import uuid

from app.review_cancellation import cancel_ineligible_reviews


class ReviewCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.active = {}
        self.stopped = []
        for _ in range(3):
            job = {'id': uuid.uuid4(), 'attempt_id': uuid.uuid4()}
            async def model(identity=job['id']):
                try:
                    await asyncio.Event().wait()
                finally:
                    self.stopped.append(identity)
            task = asyncio.create_task(model())
            self.active[task] = job
        await asyncio.sleep(0)

    async def asyncTearDown(self):
        for task in self.active:
            task.cancel()
        await asyncio.gather(*self.active, return_exceptions=True)

    def rows(self):
        return [dict(job, state='running', leased=True, eligible=True) for job in self.active.values()]

    async def test_only_revoked_attempts_cancel_and_drain(self):
        rows = self.rows()
        rows[0]['eligible'] = False
        rows[1]['attempt_id'] = uuid.uuid4()
        pool = SimpleNamespace(fetch=AsyncMock(return_value=rows))
        await cancel_ineligible_reviews(pool, self.active)
        self.assertEqual([True, True, False], [t.cancelled() for t in self.active])
        self.assertCountEqual([r['id'] for r in rows[:2]], self.stopped)
        self.assertEqual(1, pool.fetch.await_count)

    async def test_failed_deleted_and_missing_rows_release_slots(self):
        rows = self.rows()
        rows[0]['state'] = 'failed'
        rows[1]['leased'] = False
        await cancel_ineligible_reviews(SimpleNamespace(fetch=AsyncMock(return_value=rows[:2])), self.active)
        self.assertTrue(all(t.cancelled() for t in self.active))

    async def test_completed_review_cleanup_and_valid_leases_are_preserved(self):
        rows = self.rows()
        rows[0].update(state='succeeded', leased=False)
        rows[1].update(state='unresolved', leased=False)
        await cancel_ineligible_reviews(SimpleNamespace(fetch=AsyncMock(return_value=rows)), self.active)
        self.assertFalse(any(t.done() for t in self.active))

    async def test_database_failure_does_not_evict_valid_reviews(self):
        with self.assertLogs('platform.review-worker', level='WARNING'):
            await cancel_ineligible_reviews(SimpleNamespace(fetch=AsyncMock(side_effect=ConnectionError('private'))), self.active)
        self.assertFalse(any(t.done() for t in self.active))
