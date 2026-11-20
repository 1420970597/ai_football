"""Wrong quote IDs must not hide an already-read exact intent."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import unittest
import uuid

from app import worker
from app.review_completion import completion_issues,continuation_prompt
from app.review_summary import record_summary
from app.review_state import trace
from app.review_tool_evidence import snapshot_result
from app.review_intent_retry import intent_matches_quote
import test_reviews as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires task-owned isolated PostgreSQL')
class QuoteIdIntentRetryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture
    search_all=support.ReviewTests.search_all
    resolve=support.ReviewTests.resolve

    async def prepare(self,*,correct_status='active',correct_period='full_time'):
        markets=[dict(market_id='4',market_name='全场让球',kind='handicap',period=period,
                      line=line,selection='home',price='2.21',status=status,
                      source_updated_at=self.now.isoformat(),received_at=self.now.isoformat())
                 for line,status,period in (('.75','active','full_time'),('1.25',correct_status,correct_period))]
        self.fixture_id=await self.fixture(1,id='leyu:77001',markets=markets)
        key,_=await self.search_all();await self.resolve(key)
        result=await self.post('list_match_markets',fixture_id=self.fixture_id)
        self.assertEqual(200,result.status_code,result.text)
        quotes=result.json()['markets']
        self.wrong=next(q for q in quotes if float(q['line'])==.75)
        self.correct=next(q for q in quotes if float(q['line'])==1.25)
        rejected=await self.record(self.wrong)
        self.assertEqual('source_line_intent_conflict',rejected['rejected'][0]['reason'])
        return rejected

    async def record(self,quote,evidence='Alpha1 +1/1.5 @1.79，全场让球'):
        response=await self.post('record_expert_signals',signals=[dict(fixture_id=self.fixture_id,
            quote_id=quote['quote_id'],evidence_text=evidence)])
        self.assertEqual(200,response.status_code,response.text)
        return response.json()

    async def issues(self):
        async with self.pool.acquire() as conn:
            return await completion_issues(conn,self.job,self.attempt,set())

    async def test_exact_read_option_requests_correction_without_recording(self):
        rejected=await self.prepare()
        diagnostic=rejected['rejected'][0].get('intent_context')
        self.assertIsInstance(diagnostic,dict)
        self.assertEqual('1.25',diagnostic['source_intent']['selection_line'])
        self.assertEqual('.75',str(float(diagnostic['selected_selection_line'])).lstrip('0'))
        issues=await self.issues()
        self.assertEqual(1,issues['line_intent_retry_count'])
        self.assertIn('quote_id',continuation_prompt(issues))
        for table in ('v2_expert_signals','v2_quote_evidence','v2_bet_orders'):
            self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM '+table))

    async def test_correct_acceptance_clears_original_wrong_id_rejection(self):
        await self.prepare()
        self.assertEqual(1,(await self.issues())['line_intent_retry_count'])
        accepted=await self.record(self.correct)
        self.assertEqual(1,len(accepted['accepted']))
        self.assertEqual({},await self.issues())
        async with self.pool.acquire() as conn,conn.transaction():
            await trace(conn,self.job,'hermes_result',{'attempt_id':str(self.attempt),'content':'已纠正录入'})
            await record_summary(conn,dict(id=self.job,message_id=self.message),self.attempt,'real_time')
        receipt=await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertTrue(receipt['completion_verified'])
        self.assertEqual(1,receipt['accepted_count'])
        self.assertNotIn('未解决',receipt['content'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_other_line_acceptance_does_not_clear_expected_intent(self):
        await self.prepare()
        self.assertEqual(1,len((await self.record(self.wrong,'Alpha1 +0.75'))['accepted']))
        self.assertEqual(1,(await self.issues())['line_intent_retry_count'])

    async def test_closed_or_other_period_exact_option_does_not_trigger_futile_retry(self):
        await self.prepare(correct_status='closed')
        self.assertEqual({},await self.issues())
        await self.pool.execute("UPDATE v2_current_quotes SET status='active',period='first_half' WHERE id=$1",
                                uuid.UUID(self.correct['quote_id']))
        self.assertEqual({},await self.issues())

    async def test_old_attempt_rejection_cannot_force_current_continuation(self):
        await self.prepare()
        await self.pool.execute("UPDATE v2_analysis_traces SET payload=jsonb_set(payload,'{attempt_id}',to_jsonb($2::text)) WHERE job_id=$1",
                                self.job,str(uuid.uuid4()))
        self.assertEqual({},await self.issues())

    async def test_old_attempt_acceptance_does_not_clear_current_rejection(self):
        await self.prepare()
        await self.record(self.correct)
        await self.pool.execute("UPDATE v2_analysis_traces SET payload=jsonb_set(payload,'{attempt_id}',to_jsonb($2::text)) "
                                "WHERE job_id=$1 AND trace_kind='mcp_record_expert_signals' "
                                "AND jsonb_array_length(payload->'accepted')>0",self.job,str(uuid.uuid4()))
        self.assertEqual(1,(await self.issues())['line_intent_retry_count'])

    async def test_unread_exact_option_cannot_create_a_retry_hint(self):
        await self.prepare()
        await self.pool.execute('DELETE FROM v2_review_market_reads WHERE job_id=$1 AND quote_id=$2',
                                self.job,uuid.UUID(self.correct['quote_id']))
        self.assertEqual({},await self.issues())

    async def test_other_team_same_selected_line_does_not_clear_intent(self):
        await self.prepare()
        await self.pool.execute("UPDATE v2_current_quotes SET line=-1.25,selection='away' WHERE id=$1",
                                uuid.UUID(self.correct['quote_id']))
        self.assertEqual({},await self.issues())

    async def test_total_direction_is_corrected_without_odds_comparison(self):
        self.fixture_id=await self.fixture(1,markets=[dict(market_id='total',market_name='全场大小',
            kind='total',period='full_time',line='2.5',selection=side,price='2.21',status='active',
            source_updated_at=self.now.isoformat(),received_at=self.now.isoformat())for side in ('under','over')])
        key,_=await self.search_all();await self.resolve(key)
        response=await self.post('list_match_markets',fixture_id=self.fixture_id)
        self.assertEqual(200,response.status_code,response.text)
        quotes=response.json()['markets'];under=next(q for q in quotes if q['selection']=='under')
        over=next(q for q in quotes if q['selection']=='over')
        rejected=await self.record(under,'全场大2.5 @9.99')
        self.assertEqual('source_selection_intent_conflict',rejected['rejected'][0]['reason'])
        self.assertEqual(1,(await self.issues())['line_intent_retry_count'])
        self.assertEqual(1,len((await self.record(over,'全场大2.5 @9.99'))['accepted']))
        self.assertEqual({},await self.issues())

    async def test_malformed_legacy_intent_is_not_cast_as_numeric(self):
        await self.prepare()
        await self.pool.execute("UPDATE v2_analysis_traces SET payload=jsonb_set(payload,"
            "'{rejected,0,intent_context,source_intent,selection_line}','\"bad\"'::jsonb) "
            "WHERE job_id=$1 AND trace_kind='mcp_record_expert_signals'",self.job)
        self.assertEqual({},await self.issues())

    async def test_bounded_same_session_followup_corrects_quote_id(self):
        await self.prepare()
        async def answer(*args,**kwargs):
            if kwargs.get('continuation_round'):await self.record(self.correct)
            return '已完成本项核验',{'content':'已完成本项核验'}
        job=dict(id=self.job,attempt_id=self.attempt,message={},review_mode='real_time')
        with patch.object(worker,'review_message',AsyncMock(side_effect=answer))as model:
            result=(await worker._review_until_verified(self.pool,SimpleNamespace(),job))[1]
        self.assertEqual(2,model.await_count)
        self.assertTrue(result['completion_verified'])

    async def test_repeated_wrong_id_stays_unverified_after_three_rounds(self):
        await self.prepare()
        job=dict(id=self.job,attempt_id=self.attempt,message={},review_mode='real_time')
        with patch.object(worker,'review_message',AsyncMock(return_value=('未解决',{'content':'未解决'})))as model:
            result=(await worker._review_until_verified(self.pool,SimpleNamespace(),job))[1]
        self.assertEqual(3,model.await_count)
        self.assertFalse(result['completion_verified'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))


class IntentDiagnosticWireTests(unittest.TestCase):
    def test_selected_line_uses_only_verified_native_home_basis(self):
        context={'fixture_id':'leyu:1','market_id':'4','market_kind':'handicap','period':'full_time',
                 'selection':'away','source_intent':{'selection_line':'-1.25'}}
        native={k:v for k,v in context.items()if k!='source_intent'}|{'line':'1.25'}
        self.assertTrue(intent_matches_quote(context,native))
        self.assertFalse(intent_matches_quote(context,native|{'selection':'home'}))
        self.assertFalse(intent_matches_quote(context|{'period':'first_half'},native))
        generic=context|{'fixture_id':'other:1'}
        self.assertFalse(intent_matches_quote(generic,native|{'fixture_id':'other:1'}))
        self.assertTrue(intent_matches_quote(generic,native|{'fixture_id':'other:1','line':'-1.25'}))

    def test_nested_diagnostic_has_an_explicit_whitelist(self):
        source={'expert':'expert','accepted':[],'rejected':[{'index':0,'reason':'source_line_intent_conflict',
            'intent_context':{'fixture_id':'leyu:1','market_id':'4','market_kind':'handicap','period':'full_time',
                'source_intent':{'selection_line':'1.25','private':'hidden'},'selected_selection_line':'.75','private':'hidden'}}]}
        wire=snapshot_result('mcp_record_expert_signals',source)
        context=wire['rejected'][0].get('intent_context')
        self.assertIsInstance(context,dict)
        self.assertNotIn('private',context)
        self.assertNotIn('private',context['source_intent'])
