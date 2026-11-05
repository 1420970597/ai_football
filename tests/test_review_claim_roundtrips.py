"""Claim optimization keeps transaction boundaries and committed sequences."""
import asyncio
from contextlib import asynccontextmanager
import os
import unittest
from unittest.mock import patch

from app import worker
import test_reviews as support


class CountedPool:
    def __init__(self, pool):
        self.pool, self.queries = pool, []

    @asynccontextmanager
    async def acquire(self):
        async with self.pool.acquire() as conn:
            queries = self.queries

            class Connection:
                def __getattr__(self, name):
                    method = getattr(conn, name)
                    if name not in {'fetch', 'fetchrow', 'fetchval', 'execute'}:
                        return method

                    async def counted(query, *args, **kwargs):
                        queries.append(query)
                        return await method(query, *args, **kwargs)
                    return counted

            yield Connection()


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class ClaimRoundTripTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown

    async def queue(self):
        await self.pool.execute(
            "UPDATE v2_analysis_jobs SET state='queued',attempts=0,attempt_id=NULL,lease_until=NULL WHERE id=$1",
            self.job)

    async def test_claim_five_statements_reuses_activation_lock(self):
        await self.queue()
        counted = CountedPool(self.pool)
        with patch.object(worker, 'HISTORICAL_CONCURRENCY', 0):
            claimed = await worker.claim_job(counted)
        self.assertEqual(self.job, claimed['id'])
        self.assertEqual(5, len(counted.queries))
        self.assertIn('scheduler_gate AS MATERIALIZED', counted.queries[0])
        self.assertIn('v2-review-trace:', counted.queries[-2])
        self.assertIn('INSERT INTO v2_analysis_traces', counted.queries[-1])
        self.assertFalse(any(q.startswith('SELECT pg_advisory_xact_lock') for q in counted.queries))
        self.assertNotIn('trace_lock', claimed['message'])
        row = await self.pool.fetchrow(
            'SELECT sequence_no,payload FROM v2_analysis_traces WHERE job_id=$1', self.job)
        self.assertEqual(1, row['sequence_no'])
        self.assertEqual(str(claimed['attempt_id']), row['payload']['attempt_id'])
        self.assertEqual('fresh_real_time', row['payload']['scheduler_lane'])

    async def test_activation_waits_then_claim_reads_committed_trace_sequence(self):
        await self.queue()
        # A newly inserted trace holds its job FK lock until commit, so the
        # scheduler correctly skips that job. Commit the prior receipt first;
        # hold only the sequence lock to exercise activation's wait path.
        async with self.pool.acquire() as conn, conn.transaction():
            await worker.trace(conn, self.job, 'previous_receipt', {'note': 'committed prior receipt'})
        claim = None
        try:
            async with self.pool.acquire() as holder, holder.transaction():
                pid = await holder.fetchval('SELECT pg_backend_pid()')
                await holder.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended('v2-review-trace:' || $1::text,0))",
                    str(self.job))
                with patch.object(worker, 'HISTORICAL_CONCURRENCY', 0):
                    claim = asyncio.create_task(worker.claim_job(self.pool))
                    blocked = False
                    for _ in range(100):
                        blocked = await self.pool.fetchval(
                            'SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() '
                            'AND $1=ANY(pg_blocking_pids(pid)))', pid)
                        if blocked:
                            break
                        await asyncio.sleep(.01)
                    self.assertTrue(blocked, 'activation must acquire the existing trace lock')
                    self.assertFalse(claim.done())
            result = await asyncio.wait_for(claim, 5)
        finally:
            if claim is not None and not claim.done():
                claim.cancel()
                await asyncio.gather(claim, return_exceptions=True)
        self.assertEqual(self.job, result['id'])
        rows = await self.pool.fetch(
            'SELECT sequence_no,trace_kind,payload FROM v2_analysis_traces WHERE job_id=$1 ORDER BY sequence_no',
            self.job)
        self.assertEqual([(1, 'previous_receipt'), (2, 'attempt_claimed')],
                         [(r['sequence_no'], r['trace_kind']) for r in rows])
        self.assertEqual(str(result['attempt_id']), rows[1]['payload']['attempt_id'])

    async def test_contended_setup_does_not_expire_jobs_or_initialize_pairs(self):
        await self.pool.execute(
            "UPDATE v2_analysis_jobs SET attempts=$2,lease_until=now()-interval '1 second' WHERE id=$1",
            self.job, worker.MAX_ATTEMPTS)
        before = await self.pool.fetchrow(
            'SELECT state,attempts,attempt_id,lease_until,last_error FROM v2_analysis_jobs WHERE id=$1', self.job)
        async with self.pool.acquire() as holder, holder.transaction():
            self.assertTrue(await worker._scheduler_lock(holder))
            counted = CountedPool(self.pool)
            self.assertIsNone(await asyncio.wait_for(worker.claim_job(counted), 2))
            self.assertEqual(1, len(counted.queries))
            self.assertEqual(before, await self.pool.fetchrow(
                'SELECT state,attempts,attempt_id,lease_until,last_error FROM v2_analysis_jobs WHERE id=$1', self.job))
            for table in ('v2_review_source_schedule', 'v2_review_expert_schedule', 'v2_analysis_traces'):
                self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM '+table))

    async def test_gated_setup_keeps_transaction_budgets_and_rolls_back(self):
        class AbortSetup(Exception):
            pass
        async with self.pool.acquire() as conn:
            with self.assertRaises(AbortSetup):
                async with conn.transaction():
                    self.assertEqual(0, await worker.prepare_claim_schedule(
                        conn, worker.MAX_ATTEMPTS, worker.EXHAUSTED_BATCH_SIZE,
                        worker.PERSISTENT_RETRY_FAILURES, scheduler_lock=worker.SCHEDULER_LOCK))
                    self.assertEqual('2s', await conn.fetchval('SHOW lock_timeout'))
                    self.assertEqual('3s', await conn.fetchval('SHOW statement_timeout'))
                    self.assertEqual(1, await conn.fetchval('SELECT count(*) FROM v2_review_source_schedule'))
                    raise AbortSetup()
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_source_schedule'))
        async with self.pool.acquire() as conn, conn.transaction():
            self.assertTrue(await worker._scheduler_lock(conn), 'aborted setup must release the advisory lock')

    async def test_trace_insert_failure_rolls_back_claim_and_releases_locks(self):
        await self.queue()
        before = await self.pool.fetchrow(
            'SELECT state,attempts,attempt_id,lease_until FROM v2_analysis_jobs WHERE id=$1', self.job)
        await self.pool.execute(
            "CREATE FUNCTION reject_claim_trace() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "IF NEW.trace_kind='attempt_claimed' THEN RAISE EXCEPTION 'isolated claim trace failure'; END IF; "
            'RETURN NEW; END $$')
        await self.pool.execute(
            'CREATE TRIGGER reject_claim_trace BEFORE INSERT ON v2_analysis_traces '
            'FOR EACH ROW EXECUTE FUNCTION reject_claim_trace()')
        with patch.object(worker, 'HISTORICAL_CONCURRENCY', 0):
            with self.assertRaisesRegex(Exception, 'isolated claim trace failure'):
                await worker.claim_job(self.pool)
        self.assertEqual(before, await self.pool.fetchrow(
            'SELECT state,attempts,attempt_id,lease_until FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1', self.job))
        await self.pool.execute('DROP TRIGGER reject_claim_trace ON v2_analysis_traces')
        with patch.object(worker, 'HISTORICAL_CONCURRENCY', 0):
            claimed = await asyncio.wait_for(worker.claim_job(self.pool), 5)
        self.assertEqual(self.job, claimed['id'])
        self.assertEqual(1, await self.pool.fetchval(
            'SELECT sequence_no FROM v2_analysis_traces WHERE job_id=$1', self.job))
