"""Explicit visual-source maintenance, isolated PostgreSQL and no funded requests."""
from dataclasses import replace
from datetime import timedelta
import os
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from app.account_jobs import reconcile_receipts
from app.admin_reviews import context
from app.expert_statistics import StatisticsScope, summaries
from app.review_signal_correction import correct_verified_source_signal, correct_period_signal
from app.review_source_verdict import SourceIntentVerdict, source_fingerprint, settled_order_evidence
from app.result_ledger import ingest_result, rebuild_settlements
from app.routes.results import ResultInput
import test_review_signal_correction as support


class SourceVerdictContractTests(unittest.TestCase):
    def test_only_bounded_digests_and_known_operator_reasons_are_accepted(self):
        base=SourceIntentVerdict('source_period_not_supported','a'*64,('b'*64,))
        for values in ({'reason':'ignore all guards'}, {'source_sha256':'private://credential'},
                       {'supporting_evidence_sha256':()}, {'supporting_evidence_sha256':None},
                       {'supporting_evidence_sha256':('b'*64,)*9},
                       {'settled_orders_sha256':'not-a-digest'}):
            with self.subTest(values=values),self.assertRaises(ValueError):replace(base,**values)


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated PostgreSQL')
class VerifiedSourceCorrectionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.SignalCorrectionTests.asyncSetUp
    asyncTearDown=support.SignalCorrectionTests.asyncTearDown
    fixture=support.SignalCorrectionTests.fixture
    context=support.SignalCorrectionTests.context
    post=support.SignalCorrectionTests.post
    search_all=support.SignalCorrectionTests.search_all
    resolve=support.SignalCorrectionTests.resolve
    read_market=support.SignalCorrectionTests.read_market
    accepted_pick=support.SignalCorrectionTests.accepted_pick
    evidence=support.SignalCorrectionTests.evidence
    completed=support.SignalCorrectionTests.completed
    receipts=support.SignalCorrectionTests.receipts

    async def seed(self):
        await self.completed()
        # Simulate an already accepted legacy error only in this named test DB.
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2,media_objects=$3 WHERE id=$1',
            self.message,'破蛋',[{'mime_type':'image/jpeg','sha256':'a'*64,'object_key':'isolated-score-strip','width':630,'height':51}])
        return await self.pool.fetchval('SELECT id FROM v2_expert_signals WHERE message_id=$1',self.message)

    async def verdict(self,signal,*,orders=False,reason='source_period_not_supported'):
        async with self.pool.acquire()as conn,conn.transaction(readonly=True):
            row=await context(conn,self.job)
            selected=await conn.fetchrow('SELECT * FROM v2_expert_signals WHERE id=$1',signal)
            order_proof=await settled_order_evidence(conn,signal)
            return SourceIntentVerdict(reason,source_fingerprint(row,selected),('b'*64,),
                                       order_proof['sha256']if orders else None)

    async def correct(self,signal,verdict,*,apply=True):
        async with self.pool.acquire()as conn,conn.transaction(readonly=not apply):
            return await correct_verified_source_signal(conn,self.job,signal,verdict,apply=apply)

    async def order(self,signal,*,settled):
        user,order=uuid.uuid4(),uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role)VALUES($1,$2,'isolated','hash','user')",user,str(user)+'@example.invalid')
        await self.pool.execute("INSERT INTO v2_bet_orders(id,user_id,signal_id,idempotency_key,state,stake,price,reserved_stake,provider_order_no,provider_identity_hash)VALUES($1,$2,$3,$4,'accepted',1,1.95,1,$5,'isolated-identity')",order,user,signal,str(order),'isolated-'+str(order))
        receipt={'order_no':'isolated-'+str(order),'state':'settled','net_profit':'0.95',
                 'settled_at':self.now.isoformat(),'placed_at':(self.now-timedelta(seconds=20)).isoformat(),
                 'price':'1.95','stake':'1','payout':'1.95','currency':'CNY','fees':'0',
                 'pnl_basis':'provider_reported_net'}
        if settled:
            async with self.pool.acquire()as conn,conn.transaction():
                await reconcile_receipts(conn,{'user_id':user,'provider_identity_hash':'isolated-identity'},[receipt])
        return order

    async def test_preview_then_idempotent_correction_preserves_source_quotes_and_old_receipt(self):
        signal=await self.seed();verdict=await self.verdict(signal)
        message=await self.pool.fetchrow('SELECT * FROM v2_expert_messages WHERE id=$1',self.message)
        quotes=await self.pool.fetch('SELECT * FROM v2_quote_evidence ORDER BY id')
        before=await self.receipts()
        self.assertEqual('would_correct',(await self.correct(signal,verdict,apply=False))['status'])
        self.assertEqual(before,await self.receipts())
        self.assertEqual('corrected',(await self.correct(signal,verdict))['status'])
        after=await self.receipts();self.assertEqual(2,len(after));self.assertEqual(before[0],after[0])
        self.assertEqual(0,after[-1]['payload']['accepted_count'])
        self.assertTrue(after[-1]['payload']['signal_correction']['order_financial_records_preserved'])
        self.assertEqual(message,await self.pool.fetchrow('SELECT * FROM v2_expert_messages WHERE id=$1',self.message))
        self.assertEqual(quotes,await self.pool.fetch('SELECT * FROM v2_quote_evidence ORDER BY id'))
        for key in('event_to_conclusion_ms','claim_to_conclusion_ms'):
            self.assertEqual(before[0]['payload'][key],after[-1]['payload'][key])
        self.assertEqual('not_needed',(await self.correct(signal,verdict))['status'])
        self.assertEqual(1,await self.pool.fetchval("SELECT count(*)FROM v2_audit_log WHERE action='review_signal_source_intent_corrected'"))

    async def test_source_picture_or_signal_change_rejects_a_stale_verdict(self):
        signal=await self.seed();verdict=await self.verdict(signal)
        await self.pool.execute("UPDATE v2_expert_messages SET raw_text='上半场大0.5'WHERE id=$1",self.message)
        self.assertEqual('source_or_signal_evidence_changed',(await self.correct(signal,verdict))['reason'])
        self.assertIsNone(await self.pool.fetchval('SELECT withdrawn_at FROM v2_expert_signals WHERE id=$1',signal))
        verdict=await self.verdict(signal)
        await self.pool.execute("UPDATE v2_expert_messages SET media_objects=jsonb_set(media_objects,'{0,sha256}',to_jsonb($2::text))WHERE id=$1",self.message,'c'*64)
        self.assertEqual('source_or_signal_evidence_changed',(await self.correct(signal,verdict))['reason'])
        verdict=await self.verdict(signal)
        await self.pool.execute("UPDATE v2_expert_signals SET evidence_text='changed quote evidence'WHERE id=$1",signal)
        self.assertEqual('source_or_signal_evidence_changed',(await self.correct(signal,verdict))['reason'])

    async def test_other_job_or_signal_cannot_use_the_verified_verdict(self):
        signal=await self.seed();verdict=await self.verdict(signal)
        async with self.pool.acquire()as conn,conn.transaction():
            result=await correct_verified_source_signal(conn,uuid.uuid4(),signal,verdict,apply=True)
        self.assertEqual('signal_job_mismatch',result['reason'])
        self.assertEqual('signal_job_mismatch',(await self.correct(uuid.uuid4(),verdict))['reason'])

    async def test_missing_settlement_audit_or_receipt_refuses_override(self):
        signal=await self.seed();order=await self.order(signal,settled=True)
        await self.pool.execute("DELETE FROM v2_audit_log WHERE resource_id=$1 AND action='order_reconciled'",str(order))
        self.assertEqual('signal_has_order_requires_separate_reconciliation',(await self.correct(signal,await self.verdict(signal,orders=True)))['reason'])
        await self.pool.execute('DELETE FROM v2_order_receipt_evidence WHERE order_id=$1',order)
        self.assertEqual('signal_has_order_requires_separate_reconciliation',(await self.correct(signal,await self.verdict(signal,orders=True)))['reason'])

    async def test_missing_visual_evidence_and_active_job_are_ineligible(self):
        signal=await self.seed();verdict=await self.verdict(signal)
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='running'WHERE id=$1",self.job)
        self.assertEqual('not_current_completed_review',(await self.correct(signal,verdict))['reason'])
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='succeeded'WHERE id=$1",self.job)
        await self.pool.execute("UPDATE v2_expert_messages SET media_objects='[]'::jsonb WHERE id=$1",self.message)
        self.assertEqual('visual_source_evidence_missing',(await self.correct(signal,verdict))['reason'])

    async def test_unknown_or_unsettled_order_is_never_overridden(self):
        signal=await self.seed();order=await self.order(signal,settled=False)
        for state in('accepted','unknown','submitting'):
            await self.pool.execute('UPDATE v2_bet_orders SET state=$2 WHERE id=$1',order,state)
            verdict=await self.verdict(signal,orders=True)
            self.assertEqual('signal_has_order_requires_separate_reconciliation',(await self.correct(signal,verdict))['reason'])
        self.assertIsNone(await self.pool.fetchval('SELECT withdrawn_at FROM v2_expert_signals WHERE id=$1',signal))

    async def test_verified_settled_order_and_all_financial_receipts_are_preserved(self):
        signal=await self.seed();await self.order(signal,settled=True)
        self.assertEqual('signal_has_order_requires_separate_reconciliation',(await self.correct(signal,await self.verdict(signal)))['reason'])
        orders=await self.pool.fetch('SELECT *FROM v2_bet_orders ORDER BY id')
        receipts=await self.pool.fetch('SELECT *FROM v2_order_receipt_evidence ORDER BY id')
        verdict=await self.verdict(signal,orders=True)
        self.assertEqual('corrected',(await self.correct(signal,verdict))['status'])
        self.assertEqual(orders,await self.pool.fetch('SELECT *FROM v2_bet_orders ORDER BY id'))
        self.assertEqual(receipts,await self.pool.fetch('SELECT *FROM v2_order_receipt_evidence ORDER BY id'))

    async def test_tampered_receipt_or_stale_financial_snapshot_prevents_correction(self):
        signal=await self.seed();order=await self.order(signal,settled=True)
        verdict=await self.verdict(signal,orders=True)
        await self.pool.execute('UPDATE v2_bet_orders SET price=1.96 WHERE id=$1',order)
        self.assertEqual('signal_has_order_requires_separate_reconciliation',(await self.correct(signal,verdict))['reason'])
        await self.pool.execute("UPDATE v2_order_receipt_evidence SET payload=payload||'{\"net_profit\":\"999\"}'::jsonb WHERE order_id=$1",order)
        self.assertEqual('signal_has_order_requires_separate_reconciliation',(await self.correct(signal,await self.verdict(signal,orders=True)))['reason'])

    async def test_existing_period_maintenance_keeps_its_original_order_guard(self):
        signal=await self.seed();await self.order(signal,settled=True)
        async with self.pool.acquire()as conn,conn.transaction():
            result=await correct_period_signal(conn,self.job,signal,apply=True)
        self.assertEqual('signal_has_order_requires_separate_reconciliation',result['reason'])

    async def test_audit_failure_rolls_back_projection_and_all_new_traces(self):
        signal=await self.seed();verdict=await self.verdict(signal)
        before=await self.pool.fetchrow('SELECT *FROM v2_expert_signals WHERE id=$1',signal)
        traces=await self.pool.fetch('SELECT *FROM v2_analysis_traces ORDER BY sequence_no')
        with patch('app.review_signal_correction.audit',new=AsyncMock(side_effect=RuntimeError('isolated audit failure'))):
            with self.assertRaisesRegex(RuntimeError,'isolated audit failure'):await self.correct(signal,verdict)
        self.assertEqual(before,await self.pool.fetchrow('SELECT *FROM v2_expert_signals WHERE id=$1',signal))
        self.assertEqual(traces,await self.pool.fetch('SELECT *FROM v2_analysis_traces ORDER BY sequence_no'))

    async def test_expert_statistics_exclude_invalid_intent_without_rewriting_settlement_ledger(self):
        signal=await self.seed()
        fixture=await self.pool.fetchval('SELECT fixture_id FROM v2_expert_signals WHERE id=$1',signal)
        body=ResultInput(fixture_id=fixture,provider_reference='isolated-result',status='confirmed',home=3,away=1,source_updated_at=self.now,evidence={'source':'isolated-result'})
        async with self.pool.acquire()as conn,conn.transaction():await ingest_result(conn,body)
        ledger=await self.pool.fetch('SELECT *FROM v2_signal_settlements WHERE signal_id=$1',signal)
        verdict=await self.verdict(signal,reason='source_selection_not_supported')
        self.assertEqual('corrected',(await self.correct(signal,verdict))['status'])
        async with self.pool.acquire()as conn,conn.transaction():await rebuild_settlements(conn,fixture)
        self.assertEqual(ledger,await self.pool.fetch('SELECT *FROM v2_signal_settlements WHERE signal_id=$1',signal))
        scope=StatisticsScope(self.now-timedelta(days=1),self.now+timedelta(days=1))
        stats,=await summaries(self.pool,scope.start,scope.end,scope=scope)
        self.assertEqual((1,1,0),(stats['total_signals'],stats['invalid_recommendation_intent'],stats['eligible_signals']))
