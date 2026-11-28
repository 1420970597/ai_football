"""A unique attempt-owned team pair can attribute a negative phase check."""
from datetime import timedelta
import os
import unicodedata
import unittest
from app import review_phase_claims
from app.review_state import trace
from app.review_summary import record_summary
import test_review_phase_completion as support


class NamedPhaseExtractionTests(unittest.TestCase):
    def claims(self,text):
        return getattr(review_phase_claims,'named_phase_claims',lambda _:[])(text)

    def test_actual_named_metadata_retains_wrong_phase_without_inventing_identity(self):
        text='水晶宫 VS 利物浦（英超，2026-11-08 14:00 UTC，赛前）：无有效报价。'
        self.assertEqual([{'content':unicodedata.normalize('NFKC',text),'claimed_phase':'scheduled'}],self.claims(text))

    def test_quotes_history_and_explicit_or_ambiguous_ids_are_not_named_claims(self):
        for text in ('> Alpha1 VS Beta1（赛前）','原文 Alpha1 VS Beta1（赛前）',
                     'Alpha1 VS Beta1，引用“phase=scheduled”。',
                     'Alpha1 VS Beta1（leyu:1，赛前）','leyu:1 与 leyu:2（赛前）'):
            self.assertEqual([],self.claims(text))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires task-owned isolated database')
class NamedPhaseCompletionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewPhaseCompletionTests.asyncSetUp
    asyncTearDown=support.ReviewPhaseCompletionTests.asyncTearDown
    context=support.ReviewPhaseCompletionTests.context
    post=support.ReviewPhaseCompletionTests.post
    fixture=support.ReviewPhaseCompletionTests.fixture
    prepare=support.ReviewPhaseCompletionTests.prepare
    review=support.ReviewPhaseCompletionTests.review

    async def test_wrong_named_metadata_cannot_finish_after_three_answers(self):
        await self.prepare()
        text='已唯一匹配 Alpha1 VS Beta1（League，14:00 UTC，赛前）：无有效报价。'
        (_,summary),model=await self.review([text]*3)
        self.assertFalse(summary['completion_verified'])
        self.assertEqual(3,model.await_count)
        conflict=summary['completion_issues']['phase_claim_conflicts'][0]
        self.assertEqual(('leyu:1','scheduled','live'),
                         (conflict['fixture_id'],conflict['claimed_phase'],conflict['actual_phase']))

    async def test_correct_named_metadata_does_not_add_model_turn(self):
        await self.prepare()
        (_,summary),model=await self.review(['已唯一匹配 Alpha1 VS Beta1（League，滚球）：无报价。'])
        self.assertTrue(summary['completion_verified']);self.assertEqual(1,model.await_count)

    async def test_named_phase_correction_reuses_binding_without_signals_or_orders(self):
        await self.prepare()
        (_,summary),model=await self.review(['已唯一匹配 Alpha1 VS Beta1（赛前）：无报价。',
                                            '已唯一匹配 Alpha1 VS Beta1（滚球）：无报价。'])
        self.assertTrue(summary['completion_verified']);self.assertEqual(2,model.await_count)
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        for table in ('v2_expert_signals','v2_bet_orders'):
            self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM '+table))

    async def test_named_claim_cannot_use_foreign_attempt_binding(self):
        await self.prepare()
        await self.pool.execute('UPDATE v2_review_fixture_bindings SET attempt_id=gen_random_uuid() WHERE job_id=$1',self.job)
        (_,summary),_=await self.review(['已唯一匹配 Alpha1 VS Beta1（赛前）：无报价。']*3)
        self.assertFalse(summary['completion_verified'])
        self.assertFalse(summary['completion_issues'].get('phase_claim_conflicts'))

    async def test_same_pair_bound_twice_is_not_arbitrarily_attributed(self):
        await self.prepare()
        await self.fixture(2,id='leyu:2',home_team='Alpha1',away_team='Beta1',phase='live',
                           kickoff_at=(self.now+timedelta(days=1)).isoformat())
        page=await self.post('search_matches',team_query='Alpha1',
                             from_utc=(self.now+timedelta(hours=12)).isoformat(),
                             to_utc=(self.now+timedelta(days=2)).isoformat())
        self.assertEqual(200,page.status_code,page.text)
        resolved=await self.post('resolve_match',search_key=page.json()['search_key'],team_a='Alpha1',team_b='Beta1')
        self.assertTrue(resolved.json()['resolved'],resolved.text)
        (_,summary),_=await self.review(['已唯一匹配 Alpha1 VS Beta1（赛前）：无报价。'])
        self.assertFalse(summary.get('completion_issues',{}).get('phase_claim_conflicts'))

    async def test_transaction_rechecks_named_phase_despite_model_success(self):
        await self.prepare()
        async with self.pool.acquire()as conn,conn.transaction():
            await trace(conn,self.job,'hermes_result',{'attempt_id':str(self.attempt),
                'content':'已唯一匹配 Alpha1 VS Beta1（赛前）：无报价。','completion_verified':True})
            await record_summary(conn,{'id':self.job,'message_id':self.message},self.attempt,'real_time')
        receipt=await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertFalse(receipt['completion_verified']);self.assertIn('阶段',receipt['content'])
