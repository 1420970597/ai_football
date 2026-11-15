import os
import unittest
from app.review_diagnostics import diagnosis,message_diagnostics
import test_reviews as support


class ReviewDiagnosisTests(unittest.TestCase):
    def test_source_cancellation_is_not_a_model_or_provider_failure(self):
        for reason in ('source_edited','source_deleted'):
            result=diagnosis({'state':'failed','last_error':reason})
            self.assertEqual(reason,result['code'])
            self.assertFalse(result['requires_attention'])

    def test_deadline_distinguishes_committed_signals_from_none_and_never_claims_completion(self):
        for count,code in ((0,'deadline_without_signals'),(2,'deadline_with_signals')):
            result=diagnosis({'state':'unresolved','last_error':'detection_deadline_exceeded','accepted_signal_count':count})
            self.assertEqual(code,result['code'])
            self.assertTrue(result['requires_attention'])
            self.assertFalse(result['completion_verified'])

    def test_model_denial_is_not_proof_of_no_recommendation(self):
        result=diagnosis({'state':'unresolved','has_model_result':True,'summary':{'completion_verified':True}})
        self.assertEqual('model_concluded_without_signal',result['code'])
        self.assertTrue(result['model_conclusion_is_not_acceptance'])
        self.assertNotIn('无推荐',result['label'])
        self.assertEqual('unresolved',diagnosis({'state':'unresolved','has_model_result':True})['code'])

    def test_incomplete_checks_win_over_search_and_market_labels(self):
        result=diagnosis({'state':'unresolved','has_searches':True,'has_market_reads':True,
            'summary':{'completion_issues':{'text_intent_unverified':True}}})
        self.assertEqual('verification_incomplete',result['code'])


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class ReviewDiagnosisDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown

    async def test_attempt_scope_prevents_old_model_receipt_from_clearing_new_failure(self):
        import uuid
        from app.review_state import append_traces
        await append_traces(self.pool,self.job,[('hermes_result',{'attempt_id':str(uuid.uuid4()),'content':'无新增推荐'}),
            ('system_review_result',{'attempt_id':str(uuid.uuid4()),'completion_verified':True})])
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='unresolved' WHERE id=$1",self.job)
        result=(await message_diagnostics(self.pool,[self.message]))[self.message]
        self.assertEqual('unresolved',result['code'])
        self.assertFalse(result['completion_verified'])

    async def test_current_receipt_and_cancellation_are_explained_without_writing_jobs(self):
        from app.review_state import append_traces
        await append_traces(self.pool,self.job,[('hermes_result',{'attempt_id':str(self.attempt),'content':'无新增推荐'}),
            ('system_review_result',{'attempt_id':str(self.attempt),'completion_verified':True})])
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='unresolved' WHERE id=$1",self.job)
        result=(await message_diagnostics(self.pool,[self.message]))[self.message]
        self.assertEqual('model_concluded_without_signal',result['code'])
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='failed',last_error='source_edited' WHERE id=$1",self.job)
        result=(await message_diagnostics(self.pool,[self.message]))[self.message]
        self.assertEqual('source_edited',result['code'])
