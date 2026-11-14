"""Real transaction contracts for skipping provably empty market recovery."""
import asyncio
from dataclasses import replace
import os
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from app import worker
from app.review_state import active_job, trace
from app.review_summary import record_summary, SUMMARY_ITEM_LIMIT
from app.review_summary_evidence import read_summary_evidence
import test_review_worker as worker_support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class RecoveryEvidenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = worker_support.ReviewWorkerTests.asyncSetUp
    asyncTearDown = worker_support.ReviewWorkerTests.asyncTearDown
    make_queued = worker_support.ReviewWorkerTests.make_queued
    queued = worker_support.ReviewWorkerTests.queued
    seed_market_receipt = worker_support.ReviewWorkerTests.seed_market_receipt

    async def claim(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        self.assertIsNotNone(job)
        return job

    async def tool_trace(self, job_id, attempt, kind, status='succeeded'):
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, job_id, kind, {'attempt_id': str(attempt), 'status': status})

    async def test_no_tools_and_search_only_use_one_snapshot_and_no_recovery_query(self):
        for search_only in (False, True):
            with self.subTest(search_only=search_only):
                job = await self.claim()
                if search_only:
                    await self.tool_trace(self.job, job['attempt_id'], 'mcp_search_matches')
                read = AsyncMock(wraps=worker.read_summary_evidence)
                recovery = AsyncMock(side_effect=AssertionError('no market receipt can recover'))
                with patch.object(worker, 'read_summary_evidence', read), \
                        patch.object(worker, 'market_recovery', recovery):
                    await worker._complete_review_once(self.pool, job, worker.time.monotonic(),
                                                     {'content': '这是一条聊天，没有新增推荐'})
                read.assert_awaited_once()
                recovery.assert_not_awaited()
                self.assertEqual('unresolved', await self.pool.fetchval(
                    'SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
                receipt = await self.pool.fetchval(
                    "SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='system_review_result' "
                    "AND payload->>'attempt_id'=$2", self.job, str(job['attempt_id']))
                self.assertEqual(0, receipt['accepted_count'])
                self.assertTrue(receipt['completion_verified'])

    async def test_any_current_market_read_or_record_receipt_keeps_original_recovery_check(self):
        for kind in ('mcp_list_match_markets', 'mcp_record_expert_signals'):
            for status in ('succeeded', 'failed'):
                with self.subTest(kind=kind, status=status):
                    job = await self.claim()
                    await self.tool_trace(self.job, job['attempt_id'], kind, status)
                    recovery = AsyncMock(wraps=worker.market_recovery)
                    with patch.object(worker, 'market_recovery', recovery):
                        await worker._complete_review_once(self.pool, job, worker.time.monotonic())
                    recovery.assert_awaited_once()
                    self.assertEqual('unresolved', await self.pool.fetchval(
                        'SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))

    async def test_old_attempt_and_other_job_market_receipts_do_not_trigger_recovery(self):
        job = await self.claim()
        await self.tool_trace(self.job, uuid.uuid4(), 'mcp_list_match_markets')
        other = (await self.queued())[0]
        await self.tool_trace(other, job['attempt_id'], 'mcp_record_expert_signals')
        recovery = AsyncMock(side_effect=AssertionError('receipts are attempt and job scoped'))
        with patch.object(worker, 'market_recovery', recovery):
            await worker._complete_review_once(self.pool, job, worker.time.monotonic())
        recovery.assert_not_awaited()

    async def test_prefetched_evidence_requires_same_locked_job_attempt_and_message(self):
        async with self.pool.acquire() as conn, conn.transaction():
            job = await active_job(conn, self.job, self.attempt, lock_trace_sequence=True)
            evidence = await read_summary_evidence(conn, self.job, self.attempt, self.message, SUMMARY_ITEM_LIMIT)
            self.assertEqual((self.job, self.attempt, self.message),
                             (evidence.job_id, evidence.attempt_id, evidence.message_id))
            self.assertFalse(evidence.has_market_tool_receipts)
            for field in ('job_id', 'attempt_id', 'message_id'):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    await record_summary(conn, job, self.attempt, 'real_time', trace_lock_held=True,
                                         evidence=replace(evidence, **{field: uuid.uuid4()}))
            with self.assertRaises(ValueError):
                await record_summary(conn, job, self.attempt, 'real_time', evidence=evidence)
            with self.assertRaises(ValueError):
                await record_summary(conn, job, self.attempt, 'real_time', trace_lock_held=True,
                                     evidence={'job_id': self.job})
            self.assertEqual(0, await conn.fetchval(
                "SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='system_review_result'"))
            self.assertIs(type(await record_summary(conn, job, self.attempt, 'real_time',
                                                   trace_lock_held=True, evidence=evidence)), int)

    async def test_market_receipt_committing_while_waiting_for_trace_lock_is_not_skipped(self):
        job = await self.claim()
        completion = None
        recovery = AsyncMock(wraps=worker.market_recovery)
        try:
            with patch.object(worker, 'market_recovery', recovery):
                async with self.pool.acquire() as holder, holder.transaction():
                    pid = await holder.fetchval('SELECT pg_backend_pid()')
                    await trace(holder, self.job, 'mcp_list_match_markets', {
                        'attempt_id': str(job['attempt_id']), 'status': 'succeeded'})
                    completion = asyncio.create_task(
                        worker._complete_review_once(self.pool, job, worker.time.monotonic()))
                    waiting = False
                    for _ in range(100):
                        waiting = await self.pool.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                            "AND $1=ANY(pg_blocking_pids(pid)))", pid)
                        if waiting:
                            break
                        await asyncio.sleep(.01)
                    self.assertTrue(waiting, 'completion must really wait for the concurrent tool trace')
                    self.assertFalse(completion.done())
                await asyncio.wait_for(completion, 5)
                recovery.assert_awaited_once()
        finally:
            if completion is not None and not completion.done():
                completion.cancel()
                await asyncio.gather(completion, return_exceptions=True)
        rows = await self.pool.fetch(
            'SELECT sequence_no,trace_kind FROM v2_analysis_traces WHERE job_id=$1 ORDER BY sequence_no', self.job)
        self.assertEqual(list(range(1, len(rows) + 1)), [row['sequence_no'] for row in rows])
        self.assertEqual(['attempt_claimed', 'mcp_list_match_markets', 'system_review_result'],
                         [row['trace_kind'] for row in rows])
