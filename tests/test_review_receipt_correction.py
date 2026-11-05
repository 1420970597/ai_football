"""Append-only correction against isolated PostgreSQL and real quote evidence."""
import asyncio
import os
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from app.review_receipt_correction import correct_recovered_receipt
from app.review_state import trace
from app.review_summary import make_summary
import test_review_summary_evidence as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class ReceiptCorrectionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.RecoveredRejectionTests.asyncSetUp
    asyncTearDown = support.RecoveredRejectionTests.asyncTearDown
    fixture = support.RecoveredRejectionTests.fixture
    context = support.RecoveredRejectionTests.context
    post = support.RecoveredRejectionTests.post
    search_all = support.RecoveredRejectionTests.search_all
    resolve = support.RecoveredRejectionTests.resolve
    read_market = support.RecoveredRejectionTests.read_market
    accepted_pick = support.RecoveredRejectionTests.accepted_pick
    evidence = support.RecoveredRejectionTests.evidence

    async def completed(self, *, other_rejection=False):
        pick, signal = await self.accepted_pick()
        rejected = [{'index': 0, 'reason': 'quote_not_read_or_changed'}]
        if other_rejection:
            rejected.append(rejected[0].copy())
            async with self.pool.acquire() as conn, conn.transaction():
                await trace(conn, self.job, 'mcp_record_expert_signals', {
                    'attempt_id': str(self.attempt), 'parameters': {'signals': [pick | {'quote_id': str(uuid.uuid4())}]},
                    'rejected': [rejected[0]]})
        evidence = await self.evidence()
        original = make_summary(evidence.signals, accepted_total=evidence.total, rejected=rejected,
                                review_mode='real_time') | {'attempt_id': str(self.attempt),
                                'outcome': 'succeeded', 'event_to_conclusion_ms': 1234.567,
                                'claim_to_conclusion_ms': 1123.456}
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'system_review_result', original)
            await conn.execute("UPDATE v2_analysis_jobs SET state='succeeded',lease_until=NULL WHERE id=$1", self.job)
        return original

    async def correct(self, apply=True):
        async with self.pool.acquire() as conn, conn.transaction(readonly=not apply):
            return await correct_recovered_receipt(conn, self.job, apply=apply)

    async def receipts(self):
        return await self.pool.fetch("SELECT id,payload FROM v2_analysis_traces WHERE job_id=$1 "
                                     "AND trace_kind='system_review_result' ORDER BY sequence_no", self.job)

    async def test_preview_then_append_preserves_original_timing_signals_and_job(self):
        original = await self.completed()
        before = await self.receipts()
        job = await self.pool.fetchrow('SELECT * FROM v2_analysis_jobs WHERE id=$1', self.job)
        signals = await self.pool.fetch('SELECT * FROM v2_expert_signals WHERE message_id=$1', self.message)
        quotes = await self.pool.fetch('SELECT * FROM v2_quote_evidence')
        self.assertEqual('would_correct', (await self.correct(False))['status'])
        self.assertEqual(before, await self.receipts())
        self.assertEqual('corrected', (await self.correct())['status'])
        after = await self.receipts()
        self.assertEqual(2, len(after))
        self.assertEqual(before[0], after[0])
        current = after[-1]['payload']
        for key in ('attempt_id', 'items', 'event_to_conclusion_ms', 'claim_to_conclusion_ms', 'outcome'):
            self.assertEqual(original[key], current[key])
        self.assertEqual({}, current['tool_rejection_counts'])
        self.assertNotIn('未解决：', current['content'])
        self.assertEqual(str(before[0]['id']), current['receipt_correction']['previous_trace_id'])
        self.assertEqual(job, await self.pool.fetchrow('SELECT * FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertEqual(signals, await self.pool.fetch('SELECT * FROM v2_expert_signals WHERE message_id=$1', self.message))
        self.assertEqual(quotes, await self.pool.fetch('SELECT * FROM v2_quote_evidence'))
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='review_receipt_corrected'"))
        self.assertEqual('not_needed', (await self.correct())['status'])
        self.assertEqual(2, len(await self.receipts()))

    async def test_other_quote_rejection_survives_partial_correction(self):
        await self.completed(other_rejection=True)
        result = await self.correct()
        self.assertEqual(1, result['removed_rejections'])
        self.assertEqual({'quote_not_read_or_changed': 1}, (await self.receipts())[-1]['payload']['tool_rejection_counts'])
        self.assertEqual('not_needed', (await self.correct())['status'])

    async def test_concurrent_corrections_append_once(self):
        await self.completed()
        results = await asyncio.gather(self.correct(), self.correct())
        self.assertEqual(['corrected', 'not_needed'], sorted(r['status'] for r in results))
        self.assertEqual(2, len(await self.receipts()))

    async def test_running_deleted_old_attempt_and_unverified_receipt_refuse(self):
        await self.completed()
        original = (await self.receipts())[0]
        for kind in ('running', 'deleted', 'attempt', 'unverified'):
            async with self.pool.acquire() as conn:
                tx = conn.transaction()
                await tx.start()
                try:
                    if kind == 'running':
                        await conn.execute("UPDATE v2_analysis_jobs SET state='running' WHERE id=$1", self.job)
                    elif kind == 'deleted':
                        await conn.execute('UPDATE v2_expert_messages SET deleted_at=now() WHERE id=$1', self.message)
                    elif kind == 'attempt':
                        await conn.execute('UPDATE v2_analysis_jobs SET attempt_id=$2 WHERE id=$1', self.job, uuid.uuid4())
                    else:
                        await conn.execute("UPDATE v2_analysis_traces SET payload=jsonb_set(payload,'{completion_verified}','false') WHERE id=$1", original['id'])
                    self.assertEqual('ineligible', (await correct_recovered_receipt(conn, self.job, apply=True))['status'], kind)
                finally:
                    await tx.rollback()
        self.assertEqual(1, len(await self.receipts()))

    async def test_newer_task_and_newer_message_revision_refuse(self):
        await self.completed()
        async with self.pool.acquire() as conn:
            for kind in ('job', 'revision'):
                tx = conn.transaction();await tx.start()
                try:
                    if kind == 'job':
                        await conn.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key) VALUES($1,$2,'failed',$3)", uuid.uuid4(), self.message, str(uuid.uuid4()))
                    else:
                        await conn.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at,revision) SELECT $2,source_id,expert_id,platform_message_id,raw_text,posted_at,revision+1 FROM v2_expert_messages WHERE id=$1", self.message, uuid.uuid4())
                    self.assertEqual('ineligible', (await correct_recovered_receipt(conn, self.job, apply=True))['status'], kind)
                finally:
                    await tx.rollback()

    async def test_removed_immutable_evidence_and_changed_signal_refuse(self):
        await self.completed()
        await self.pool.execute('UPDATE v2_expert_signals SET recommendation_price=3 WHERE message_id=$1', self.message)
        self.assertEqual('accepted_evidence_or_scope_changed', (await self.correct())['reason'])

    async def test_missing_immutable_quote_evidence_never_clears_a_rejection(self):
        await self.completed()
        await self.pool.execute('DELETE FROM v2_quote_evidence')
        self.assertEqual('not_needed', (await self.correct())['status'])
        self.assertEqual(1, len(await self.receipts()))

    async def test_audit_failure_rolls_back_appended_receipt(self):
        await self.completed()
        with patch('app.review_receipt_correction.audit', new=AsyncMock(side_effect=RuntimeError('isolated audit failure'))):
            with self.assertRaises(RuntimeError):
                await self.correct()
        self.assertEqual(1, len(await self.receipts()))
        self.assertEqual('corrected', (await self.correct())['status'])


if __name__ == '__main__':
    unittest.main()
