"""Real isolated PostgreSQL checks for explicit period correction maintenance."""
from datetime import timedelta
import os
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from app.expert_statistics import StatisticsScope, summaries
from app.result_ledger import ingest_result, rebuild_settlements
from app.routes.results import ResultInput
from app.review_signal_correction import correct_period_signal
import test_review_receipt_correction as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class SignalCorrectionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReceiptCorrectionTests.asyncSetUp
    asyncTearDown=support.ReceiptCorrectionTests.asyncTearDown
    fixture=support.ReceiptCorrectionTests.fixture
    context=support.ReceiptCorrectionTests.context
    post=support.ReceiptCorrectionTests.post
    search_all=support.ReceiptCorrectionTests.search_all
    resolve=support.ReceiptCorrectionTests.resolve
    read_market=support.ReceiptCorrectionTests.read_market
    accepted_pick=support.ReceiptCorrectionTests.accepted_pick
    evidence=support.ReceiptCorrectionTests.evidence
    completed=support.ReceiptCorrectionTests.completed
    receipts=support.ReceiptCorrectionTests.receipts

    async def legacy_signal(self):
        await self.completed()
        signal=await self.pool.fetchrow('SELECT * FROM v2_expert_signals WHERE message_id=$1',self.message)
        # Simulate a pre-guard legacy acceptance, not a production message edit.
        await self.pool.execute("UPDATE v2_expert_messages SET raw_text='半大2.5 等水上' WHERE id=$1",self.message)
        return signal['id'],signal['fixture_id']

    async def correct(self,signal,apply=True):
        async with self.pool.acquire() as conn,conn.transaction(readonly=not apply):
            return await correct_period_signal(conn,self.job,signal,apply=apply)

    async def test_preview_append_idempotency_quote_and_old_receipt_preserved(self):
        signal,fixture=await self.legacy_signal()
        before=await self.receipts()
        quotes=await self.pool.fetch('SELECT * FROM v2_quote_evidence')
        message=await self.pool.fetchrow('SELECT * FROM v2_expert_messages WHERE id=$1',self.message)
        self.assertEqual('would_correct',(await self.correct(signal,False))['status'])
        self.assertEqual(before,await self.receipts())
        self.assertEqual('corrected',(await self.correct(signal))['status'])
        after=await self.receipts()
        self.assertEqual(2,len(after));self.assertEqual(before[0],after[0])
        self.assertEqual(0,after[1]['payload']['accepted_count'])
        self.assertEqual('unresolved',after[1]['payload']['outcome'])
        for key in ('event_to_conclusion_ms','claim_to_conclusion_ms'):
            self.assertEqual(before[0]['payload'][key],after[1]['payload'][key])
        self.assertEqual(quotes,await self.pool.fetch('SELECT * FROM v2_quote_evidence'))
        self.assertEqual(message,await self.pool.fetchrow('SELECT * FROM v2_expert_messages WHERE id=$1',self.message))
        self.assertEqual('not_needed',(await self.correct(signal))['status'])
        self.assertEqual(1,await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='review_signal_period_corrected'"))
        self.assertEqual(2,len(await self.receipts()))
        scope=StatisticsScope(self.now-timedelta(days=1),self.now+timedelta(days=1))
        stats,=await summaries(self.pool,scope.start,scope.end,scope=scope)
        self.assertEqual((1,1,0),(stats['total_signals'],stats['invalid_recommendation_intent'],stats['eligible_signals']))

    async def test_verified_early_half_evidence_is_not_later_withdrawn_as_ambiguous(self):
        signal,_=await self.legacy_signal()
        for algorithm,extra in (('leyu-early-first-half-stage-v1',{}),
                                ('leyu-early-first-half-stage-v2',{'source_age_limit_seconds':300})):
            with self.subTest(algorithm=algorithm):
                await self.pool.execute("UPDATE v2_quote_evidence SET evidence=evidence || $2::jsonb "
                    'WHERE reference_id=$1 AND evidence_kind=\'expert_signal\'',signal,
                    {'source_period_context':{'algorithm':algorithm,
                        'fixture_id':'fixture-1','source_id':'fixture-1','match_state':'6',
                        'search_clock_minute':15,'quote_clock_minute':16,**extra}})
                result=await self.correct(signal,False)
                self.assertEqual(('not_needed','period_context_verified_at_acceptance'),
                                 (result['status'],result['reason']))
        self.assertIsNone(await self.pool.fetchval('SELECT withdrawn_at FROM v2_expert_signals WHERE id=$1',signal))

    async def test_already_settled_quote_and_ledger_remain_immutable_on_rebuild(self):
        signal,fixture=await self.legacy_signal()
        body=ResultInput(fixture_id=fixture,provider_reference='isolated-result',status='confirmed',
            home=3,away=1,source_updated_at=self.now,evidence={'source':'isolated-result'})
        async with self.pool.acquire() as conn,conn.transaction():
            await ingest_result(conn,body)
        ledger=await self.pool.fetch('SELECT * FROM v2_signal_settlements WHERE signal_id=$1',signal)
        self.assertEqual('win',ledger[0]['outcome'])
        self.assertEqual('corrected',(await self.correct(signal))['status'])
        async with self.pool.acquire() as conn,conn.transaction():
            await rebuild_settlements(conn,fixture)
        self.assertEqual(ledger,await self.pool.fetch('SELECT * FROM v2_signal_settlements WHERE signal_id=$1',signal))
        row=await self.pool.fetchrow('SELECT outcome,unit_net,settled_at FROM v2_expert_signals WHERE id=$1',signal)
        self.assertEqual(('pending',None,None),tuple(row.values()))

    async def test_audit_failure_rolls_back_all_projection_and_trace_writes(self):
        signal,_=await self.legacy_signal()
        before=await self.pool.fetchrow('SELECT * FROM v2_expert_signals WHERE id=$1',signal)
        traces=await self.pool.fetch('SELECT * FROM v2_analysis_traces ORDER BY sequence_no')
        with patch('app.review_signal_correction.audit',new=AsyncMock(side_effect=RuntimeError('isolated audit failure'))):
            with self.assertRaisesRegex(RuntimeError,'isolated audit failure'):
                await self.correct(signal)
        self.assertEqual(before,await self.pool.fetchrow('SELECT * FROM v2_expert_signals WHERE id=$1',signal))
        self.assertEqual(traces,await self.pool.fetch('SELECT * FROM v2_analysis_traces ORDER BY sequence_no'))

    async def test_existing_order_and_current_active_review_are_not_modified(self):
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
