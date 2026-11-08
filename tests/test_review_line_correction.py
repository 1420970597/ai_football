"""Real isolated PostgreSQL correction of source-line-invalid legacy signals."""
from datetime import timedelta
import os
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from app import review_signal_correction
from app.expert_statistics import StatisticsScope, summaries
from app.result_ledger import ingest_result, rebuild_settlements
from app.routes.results import ResultInput
import test_review_signal_correction as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class SourceLineCorrectionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.SignalCorrectionTests.asyncSetUp
    asyncTearDown = support.SignalCorrectionTests.asyncTearDown
    fixture = support.SignalCorrectionTests.fixture
    context = support.SignalCorrectionTests.context
    post = support.SignalCorrectionTests.post
    search_all = support.SignalCorrectionTests.search_all
    resolve = support.SignalCorrectionTests.resolve
    read_market = support.SignalCorrectionTests.read_market
    accepted_pick = support.SignalCorrectionTests.accepted_pick
    evidence = support.SignalCorrectionTests.evidence
    completed = support.SignalCorrectionTests.completed
    receipts = support.SignalCorrectionTests.receipts

    async def legacy_signal(self):
        await self.completed()
        row = await self.pool.fetchrow('SELECT id,fixture_id FROM v2_expert_signals WHERE message_id=$1', self.message)
        # Explicitly isolated legacy conflict; original message is not edited.
        await self.pool.execute("UPDATE v2_expert_signals SET evidence_text='大2.5/3 @1.96' WHERE id=$1",row['id'])
        return row['id'],row['fixture_id']

    async def correct(self, signal, apply=True):
        async with self.pool.acquire() as conn, conn.transaction(readonly=not apply):
            return await review_signal_correction.correct_line_signal(conn,self.job,signal,apply=apply)

    async def test_preview_append_idempotency_and_original_evidence(self):
        signal,_ = await self.legacy_signal()
        before = await self.receipts()
        quotes = await self.pool.fetch('SELECT * FROM v2_quote_evidence')
        message = await self.pool.fetchrow('SELECT * FROM v2_expert_messages WHERE id=$1',self.message)
        self.assertEqual('would_correct',(await self.correct(signal,False))['status'])
        self.assertEqual(before,await self.receipts())
        self.assertEqual('corrected',(await self.correct(signal))['status'])
        after = await self.receipts()
        self.assertEqual(2,len(after));self.assertEqual(before[0],after[0])
        self.assertEqual(0,after[-1]['payload']['accepted_count'])
        self.assertIn('盘口线',after[-1]['payload']['content'])
        self.assertEqual('review_source_line_intent_conflict',await self.pool.fetchval(
            'SELECT withdrawal_reason FROM v2_expert_signals WHERE id=$1',signal))
        self.assertEqual(quotes,await self.pool.fetch('SELECT * FROM v2_quote_evidence'))
        self.assertEqual(message,await self.pool.fetchrow('SELECT * FROM v2_expert_messages WHERE id=$1',self.message))
        for key in ('event_to_conclusion_ms','claim_to_conclusion_ms'):
            self.assertEqual(before[0]['payload'][key],after[-1]['payload'][key])
        self.assertEqual('not_needed',(await self.correct(signal))['status'])
        self.assertEqual(1,await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='review_signal_line_corrected'"))
        self.assertEqual(2,len(await self.receipts()))
        scope=StatisticsScope(self.now-timedelta(days=1),self.now+timedelta(days=1))
        row,=await summaries(self.pool,scope.start,scope.end,scope=scope)
        self.assertEqual((1,1,0),(row['total_signals'],row['invalid_recommendation_intent'],row['eligible_signals']))

    async def test_existing_order_and_active_review_refuse_correction(self):
        signal,_=await self.legacy_signal()
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='running' WHERE id=$1",self.job)
        self.assertEqual('ineligible',(await self.correct(signal))['status'])
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='succeeded' WHERE id=$1",self.job)
        user=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,'isolated@example.invalid','isolated','test-hash','user')",user)
        await self.pool.execute("INSERT INTO v2_bet_orders(id,user_id,signal_id,idempotency_key,state,stake,price) VALUES($1,$2,$3,$4,'unknown',1,1.95)",uuid.uuid4(),user,signal,str(uuid.uuid4()))
        result=await self.correct(signal)
        self.assertEqual('signal_has_order_requires_separate_reconciliation',result['reason'])
        self.assertIsNone(await self.pool.fetchval('SELECT withdrawn_at FROM v2_expert_signals WHERE id=$1',signal))

    async def test_audit_failure_rolls_back_signal_and_all_new_receipts(self):
        signal,_=await self.legacy_signal()
        before=await self.pool.fetchrow('SELECT * FROM v2_expert_signals WHERE id=$1',signal)
        traces=await self.pool.fetch('SELECT * FROM v2_analysis_traces ORDER BY sequence_no')
        with patch('app.review_signal_correction.audit',new=AsyncMock(side_effect=RuntimeError('isolated audit failure'))):
            with self.assertRaises(RuntimeError):await self.correct(signal)
        self.assertEqual(before,await self.pool.fetchrow('SELECT * FROM v2_expert_signals WHERE id=$1',signal))
        self.assertEqual(traces,await self.pool.fetch('SELECT * FROM v2_analysis_traces ORDER BY sequence_no'))

    async def test_correct_quote_is_not_withdrawn(self):
        await self.completed()
        signal=await self.pool.fetchval('SELECT id FROM v2_expert_signals WHERE message_id=$1',self.message)
        result=await self.correct(signal)
        self.assertEqual(('not_needed','no_verified_line_conflict'),
                         (result['status'],result['reason']))

    async def test_settlement_evidence_is_preserved_and_not_reprojected(self):
        signal,fixture=await self.legacy_signal()
        body=ResultInput(fixture_id=fixture,provider_reference='isolated-result',status='confirmed',
            home=3,away=1,source_updated_at=self.now,evidence={'source':'isolated-result'})
        async with self.pool.acquire() as conn,conn.transaction():await ingest_result(conn,body)
        ledger=await self.pool.fetch('SELECT * FROM v2_signal_settlements WHERE signal_id=$1',signal)
        self.assertEqual('corrected',(await self.correct(signal))['status'])
        async with self.pool.acquire() as conn,conn.transaction():await rebuild_settlements(conn,fixture)
        self.assertEqual(ledger,await self.pool.fetch('SELECT * FROM v2_signal_settlements WHERE signal_id=$1',signal))
        row=await self.pool.fetchrow('SELECT outcome,unit_net,settled_at FROM v2_expert_signals WHERE id=$1',signal)
        self.assertEqual(('pending',None,None),tuple(row.values()))
