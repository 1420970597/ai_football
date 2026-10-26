import asyncio
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest
import uuid

import test_reviews as support
from app import worker


@unittest.skipUnless(os.environ.get("TEST_DATABASE_ADMIN_URL"), "requires isolated database")
class ReviewWorkerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown

    async def make_queued(self, attempts=0):
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='queued',attempts=$2,attempt_id=NULL,lease_until=NULL WHERE id=$1", self.job, attempts)

    async def test_only_one_worker_claims_and_attempt_changes_after_expiry(self):
        await self.make_queued()
        first, second = await asyncio.gather(worker.claim_job(self.pool), worker.claim_job(self.pool))
        self.assertEqual(1, sum(item is not None for item in (first, second)))
        claimed = first or second
        await self.pool.execute("UPDATE v2_analysis_jobs SET lease_until=now()-interval '1 second' WHERE id=$1", self.job)
        retry = await worker.claim_job(self.pool)
        self.assertNotEqual(claimed["attempt_id"], retry["attempt_id"])
        self.assertEqual(2, retry["attempts"])

    async def test_final_attempt_crash_enters_dead_letter(self):
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='running',attempts=5,lease_until=now()-interval '1 second'")
        self.assertIsNone(await worker.claim_job(self.pool))
        row = await self.pool.fetchrow("SELECT state,last_error FROM v2_analysis_jobs WHERE id=$1", self.job)
        self.assertEqual("failed", row["state"])
        self.assertEqual("lease_exhausted", row["last_error"])

    async def test_incomplete_media_and_disabled_sources_are_not_claimed(self):
        await self.make_queued()
        await self.pool.execute("UPDATE v2_expert_messages SET media_state='pending'")
        self.assertIsNone(await worker.claim_job(self.pool))
        await self.pool.execute("UPDATE v2_expert_messages SET media_state='ready'")
        await self.pool.execute("UPDATE v2_expert_sources SET enabled=false")
        self.assertIsNone(await worker.claim_job(self.pool))

    async def test_model_text_without_recorded_signals_remains_unresolved(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        with patch.object(worker, "review_message", AsyncMock(return_value=("已记录盈利推荐", {"content": "已记录盈利推荐"}))), patch.object(worker, "delete_session", AsyncMock()):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model="hermes-agent"), job)
        self.assertEqual("unresolved", await self.pool.fetchval("SELECT state FROM v2_analysis_jobs WHERE id=$1", self.job))

    async def test_stale_worker_failure_cannot_overwrite_new_attempt(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        new_attempt = uuid.uuid4()
        await self.pool.execute("UPDATE v2_analysis_jobs SET attempt_id=$1 WHERE id=$2", new_attempt, self.job)
        with patch.object(worker, "delete_session", AsyncMock()):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model="hermes-agent"), job)
        row = await self.pool.fetchrow("SELECT state,attempt_id FROM v2_analysis_jobs WHERE id=$1", self.job)
        self.assertEqual("running", row["state"])
        self.assertEqual(new_attempt, row["attempt_id"])

    async def test_timeout_is_retryable_and_cleanup_runs(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        cleanup = AsyncMock()
        with patch.object(worker, "review_message", AsyncMock(side_effect=TimeoutError)), patch.object(worker, "delete_session", cleanup):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model="hermes-agent"), job)
        row = await self.pool.fetchrow("SELECT state,last_error,lease_until FROM v2_analysis_jobs WHERE id=$1", self.job)
        self.assertEqual("retry", row["state"])
        self.assertEqual("TimeoutError", row["last_error"])
        self.assertIsNone(row["lease_until"])
        cleanup.assert_awaited_once()

    async def test_heartbeat_stops_when_attempt_is_replaced(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        await self.pool.execute("UPDATE v2_analysis_jobs SET attempt_id=$1 WHERE id=$2", uuid.uuid4(), self.job)
        with patch.object(worker, "HEARTBEAT_SECONDS", 0.01):
            with self.assertRaises(worker.LeaseLost):
                await worker.keep_lease(self.pool, job)


if __name__ == "__main__":
    unittest.main()
