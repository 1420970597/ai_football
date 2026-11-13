"""Deadline ordering within the existing fair scheduler and unchanged capacity.

Uses only a task-owned PostgreSQL database. No Telegram, model or order calls.
"""
import asyncio
from datetime import timedelta
import os
import unittest
from unittest.mock import patch

from app import worker
from app.review_deadline import remaining_review_seconds
import test_review_freshness_scheduler as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires task-owned temporary PostgreSQL')
class ReviewSchedulerDeadlineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewFreshnessSchedulerTests.asyncSetUp
    asyncTearDown = support.ReviewFreshnessSchedulerTests.asyncTearDown
    new_pool = support.ReviewFreshnessSchedulerTests.new_pool
    pair = support.ReviewFreshnessSchedulerTests.pair
    queued = support.ReviewFreshnessSchedulerTests.queued
    complete = support.ReviewFreshnessSchedulerTests.complete

    async def detected(self, job, age):
        stamp = self.now - timedelta(seconds=age)
        await self.pool.execute(
            'UPDATE v2_expert_messages SET detected_at=$2 '
            'WHERE id=(SELECT message_id FROM v2_analysis_jobs WHERE id=$1)', job, stamp)
        return stamp

    async def test_known_earlier_detection_wins_before_sixty_second_aging_threshold(self):
        pair = await self.pair()
        earlier = (await self.queued(pair, age=15))[0]
        later = (await self.queued(pair, age=1))[0]
        stamp = await self.detected(earlier, 10)
        await self.detected(later, 1)
        job = await worker.claim_job(self.pool)
        self.assertEqual(earlier, job['id'])
        self.assertEqual(stamp, job['message']['detected_at'])
        self.assertGreater(remaining_review_seconds(stamp), 0)
        self.assertLessEqual(remaining_review_seconds(stamp), 10)
        self.assertEqual('queued', await self.pool.fetchval(
            'SELECT state FROM v2_analysis_jobs WHERE id=$1', later))

    async def test_detection_time_precedes_database_insertion_and_posting_recency(self):
        pair = await self.pair()
        persisted_first = (await self.queued(pair, age=1))[0]
        detected_first = (await self.queued(pair, age=15))[0]
        await self.detected(persisted_first, 1)
        await self.detected(detected_first, 10)
        self.assertEqual(detected_first, (await worker.claim_job(self.pool))['id'])

    async def test_equal_known_detection_is_fifo_without_inventing_a_new_deadline(self):
        pair = await self.pair()
        first = (await self.queued(pair, age=15))[0]
        later = (await self.queued(pair, age=1))[0]
        stamp = await self.detected(first, 5)
        await self.detected(later, 5)
        await self.pool.execute('UPDATE v2_analysis_jobs SET created_at=$2 WHERE id=$1',
                                first, self.now - timedelta(seconds=2))
        claimed = await worker.claim_job(self.pool)
        self.assertEqual(first, claimed['id'])
        self.assertEqual(stamp, claimed['message']['detected_at'])

    async def test_repeated_new_arrivals_cannot_overtake_waiter_when_one_of_two_leases_finishes(self):
        pair = await self.pair()
        active = await self.queued(pair, state='running', count=2, attempts=1)
        await self.pool.execute(
            "UPDATE v2_analysis_jobs SET lease_until=now()+interval '90 seconds' WHERE id=ANY($1::uuid[])", active)
        waiting = (await self.queued(pair, age=15))[0]
        await self.detected(waiting, 10)
        for age in (3, 2, 1):
            incoming = (await self.queued(pair, age=age))[0]
            await self.detected(incoming, age)
        with patch.object(worker, 'EXPERT_CONCURRENCY_FLOOR', 2):
            self.assertIsNone(await worker.claim_job(self.pool), 'the existing expert cap remains two')
            await self.complete({'id': active[0]})
            claimed = await worker.claim_job(self.pool)
            self.assertEqual(waiting, claimed['id'])
            self.assertIsNone(await worker.claim_job(self.pool))
        self.assertEqual(2, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_jobs WHERE state='running' AND lease_until>now()"))

    async def test_null_detection_retains_legacy_newest_first_and_remains_unknown(self):
        pair = await self.pair()
        await self.queued(pair, age=20)
        newest = (await self.queued(pair, age=1))[0]
        claimed = await worker.claim_job(self.pool)
        self.assertEqual(newest, claimed['id'])
        self.assertIsNone(claimed['message']['detected_at'])
        self.assertIsNone(remaining_review_seconds(claimed['message']['detected_at']))

    async def test_legacy_aging_still_prevents_starvation_by_known_detections(self):
        pair = await self.pair()
        legacy = (await self.queued(pair, age=120))[0]
        await self.pool.execute("UPDATE v2_analysis_jobs SET created_at=now()-interval '90 seconds' WHERE id=$1", legacy)
        for age in (3, 2, 1):
            known = (await self.queued(pair, age=age))[0]
            await self.detected(known, age)
        claimed = await worker.claim_job(self.pool)
        self.assertEqual(legacy, claimed['id'])
        self.assertIsNone(claimed['message']['detected_at'])

    async def test_source_and_expert_round_robin_survives_a_hot_experts_earlier_deadlines(self):
        hot, quiet = await self.pair(), await self.pair()
        heads = {}
        for pair, ages in ((hot, (12, 10, 8, 6)), (quiet, (5, 4, 3, 2))):
            for age in ages:
                job = (await self.queued(pair, age=age))[0]
                await self.detected(job, age)
                heads.setdefault(pair[0], job)
        seen = []
        for index in range(6):
            claimed = await worker.claim_job(self.pool)
            seen.append(claimed['source_id'])
            if index < 2:
                self.assertEqual(heads[claimed['source_id']], claimed['id'])
            await self.complete(claimed)
        for index in (0, 2, 4):
            self.assertEqual({hot[0], quiet[0]}, set(seen[index:index + 2]))

    async def test_two_per_expert_budget_is_shared_across_sources_and_pools_without_capacity_increase(self):
        first = await self.pair()
        second = await self.pair(expert=first[1])
        other = await self.pair(source=first[0])
        for pair in (first, second, other):
            for age in (8, 6, 4):
                job = (await self.queued(pair, age=age))[0]
                await self.detected(job, age)
        pool = await self.new_pool()
        claimed = []
        try:
            with patch.object(worker, 'EXPERT_CONCURRENCY_FLOOR', 2):
                for _ in range(5):
                    batch = await asyncio.gather(worker.claim_job(self.pool), worker.claim_job(pool))
                    claimed.extend(job for job in batch if job is not None)
            self.assertEqual(4, len(claimed))
            for expert in (first[1], other[1]):
                self.assertEqual(2, sum(job['expert_id'] == expert for job in claimed))
        finally:
            await pool.close()

    async def test_expired_running_attempt_recovers_before_new_deadline_and_keeps_detection(self):
        pair = await self.pair()
        expired = (await self.queued(pair, age=15, state='running', attempts=1))[0]
        stamp = await self.detected(expired, 10)
        old_attempt = await self.pool.fetchval('SELECT attempt_id FROM v2_analysis_jobs WHERE id=$1', expired)
        fresh = (await self.queued(pair, age=1))[0]
        await self.detected(fresh, 1)
        claimed = await worker.claim_job(self.pool)
        self.assertEqual(expired, claimed['id'])
        self.assertNotEqual(old_attempt, claimed['attempt_id'])
        self.assertEqual(2, claimed['attempts'])
        self.assertEqual(stamp, claimed['message']['detected_at'])
        self.assertIs(False, await worker._renew_lease(self.pool, {'id': expired, 'attempt_id': old_attempt}))

    async def test_detection_order_does_not_promote_an_older_posting_lane(self):
        pair = await self.pair()
        old_lane = (await self.queued(pair, age=worker.FRESH_PRIORITY_WINDOW_SECONDS + 30))[0]
        await self.detected(old_lane, 10)
        fresh = (await self.queued(pair, age=1))[0]
        await self.detected(fresh, 1)
        claimed = await worker.claim_job(self.pool)
        self.assertEqual(fresh, claimed['id'])
        self.assertEqual('fresh_real_time', claimed['scheduler_lane'])
