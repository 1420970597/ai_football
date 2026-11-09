"""Named identity claims need the same current tool proof as fixture IDs."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import timedelta
import unittest
import uuid

from app import worker
from app.review_completion import completion_issues, named_identity_claims
from app.review_state import trace
from app.review_summary import record_summary
import test_reviews as support


class NamedSystemPairClaimTests(unittest.TestCase):
    def test_only_asserted_system_pair_statements_enter_existing_binding_check(self):
        for content in ('对应系统比赛为 Alpha1 对 Beta1。',
                        '对应的系统对阵是 Alpha1 VS Beta1。'):
            with self.subTest(content=content):
                self.assertIn('Alpha1', named_identity_claims([content]))
        for content in ('无法确认对应系统比赛为 Alpha1 对 Beta1。',
                        '尚未确定对应的系统对阵是 Alpha1 VS Beta1。',
                        '对应系统比赛可能为 Alpha1 对 Beta1。'):
            with self.subTest(content=content):
                self.assertEqual('', named_identity_claims([content]))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires task-owned isolated database')
class NamedReviewCompletionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def prepare(self):
        await self.fixture(1, id='leyu:1', phase='finished')
        response = await self.post('search_matches', team_query='Alpha')
        self.assertEqual(200, response.status_code, response.text)
        return response.json()['search_key']

    async def issues(self, text):
        async with self.pool.acquire() as conn:
            return await completion_issues(conn, self.job, self.attempt, set(), text)

    async def test_named_claim_requires_binding_and_cannot_use_another_attempt(self):
        key = await self.prepare()
        text = '已唯一匹配 Alpha1 对 Beta1，但比赛已完场，不录入推荐。'
        issues = await self.issues(text)
        self.assertEqual(1, issues['unbound_mention_count'])
        self.assertEqual('leyu:1', issues['unbound_mentions'][0]['fixture_id'])
        self.assertEqual(key, issues['unbound_mentions'][0]['candidate']['search_key'])
        await self.pool.execute('UPDATE v2_match_searches SET attempt_id=$2 WHERE job_id=$1',
                                self.job, uuid.uuid4())
        self.assertEqual({}, await self.issues(text))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))

    async def test_system_pair_statement_without_fixture_id_requires_actual_binding(self):
        await self.prepare()
        text = '这场已识别但已完场。\n\n对应系统比赛为 Alpha1 对 Beta1，工具返回 finished，未录入。'
        job = dict(id=self.job, attempt_id=self.attempt, message={})
        with patch.object(worker, 'review_message', AsyncMock(return_value=(text, {'content': text}))) as call:
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        self.assertEqual(3, call.await_count)
        self.assertFalse(summary['completion_verified'])
        self.assertEqual(1, summary['completion_issues']['unbound_mention_count'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_real_binding_satisfies_same_system_pair_statement_in_one_turn(self):
        key = await self.prepare()
        response = await self.post('resolve_match', search_key=key, team_a='Alpha1', team_b='Beta1')
        self.assertTrue(response.json()['resolved'])
        text = '对应的系统对阵是 Alpha1 对 Beta1，已完场，无有效实时推荐。'
        job = dict(id=self.job, attempt_id=self.attempt, message={})
        with patch.object(worker, 'review_message', AsyncMock(return_value=(text, {'content': text}))) as call:
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        self.assertEqual(1, call.await_count)
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))

    async def test_final_receipt_rechecks_system_pair_statement_from_prior_turn(self):
        await self.prepare()
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'hermes_partial_result', dict(attempt_id=str(self.attempt),
                content='对应系统比赛为 Alpha1 对 Beta1，已完场。'))
            await trace(conn, self.job, 'hermes_result', dict(attempt_id=str(self.attempt),
                content='本次审核结束', completion_verified=True))
            await record_summary(conn, dict(id=self.job, message_id=self.message), self.attempt, 'real_time')
        receipt = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertFalse(receipt['completion_verified'])
        self.assertEqual(1, receipt['completion_issues']['unbound_mention_count'])

    async def test_negative_single_team_and_unsearched_pair_do_not_request_binding(self):
        await self.prepare()
        for text in ('没有找到 Alpha1 对 Beta1', '未识别 Alpha1 对 Beta1',
                     '已识别 Alpha1', '已唯一匹配 Unknown 对 Missing', '聊天，无新增推荐'):
            with self.subTest(text=text):
                self.assertEqual({}, await self.issues(text))

    async def test_later_turn_named_claim_survives_bounded_content_aggregation(self):
        await self.prepare()
        content = named_identity_claims(['已识别其他信息：' + 'x' * 20000,
                                         '已唯一匹配 Alpha1 对 Beta1。'])
        self.assertEqual(1, (await self.issues(content))['unbound_mention_count'])

    async def test_bounded_followup_retains_first_named_claim_without_forcing_binding(self):
        await self.prepare()
        first = '已按系统唯一匹配 Alpha1 对 Beta1，已完场。'
        call = AsyncMock(side_effect=[(s, {'content': s}) for s in (first, '已经完成', '未解决')])
        job = dict(id=self.job, attempt_id=self.attempt, message={})
        with patch.object(worker, 'review_message', call):
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        self.assertEqual(3, call.await_count)
        self.assertFalse(summary['completion_verified'])
        self.assertEqual(1, summary['completion_issues']['unbound_mention_count'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_followup_clears_only_after_actual_unique_tool_binding(self):
        key = await self.prepare()
        async def answer(*args, **kwargs):
            if kwargs.get('continuation_round'):
                result = await self.post('resolve_match', search_key=key, team_a='Alpha1', team_b='Beta1')
                self.assertTrue(result.json()['resolved'])
            text = '已唯一识别 Alpha1 对 Beta1，完场不能实时录入。'
            return text, {'content': text}
        job = dict(id=self.job, attempt_id=self.attempt, message={})
        with patch.object(worker, 'review_message', AsyncMock(side_effect=answer)) as call:
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        self.assertEqual(2, call.await_count)
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_transaction_receipt_rechecks_prior_named_claim(self):
        await self.prepare()
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'hermes_partial_result', dict(attempt_id=str(self.attempt),
                content='赛事已经找到：Alpha1 对 Beta1。'))
            await trace(conn, self.job, 'hermes_result', dict(attempt_id=str(self.attempt),
                content='审核完成', completion_verified=True))
            await record_summary(conn, dict(id=self.job, message_id=self.message), self.attempt, 'real_time')
        receipt = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertFalse(receipt['completion_verified'])
        self.assertEqual(1, receipt['completion_issues']['unbound_mention_count'])

    async def test_bound_pair_does_not_require_every_same_named_historical_candidate(self):
        await self.fixture(1,id='leyu:1',phase='finished')
        await self.fixture(2,id='leyu:2',phase='finished',home_team='Alpha1',away_team='Beta1',
                           kickoff_at=(self.now-timedelta(hours=3)).isoformat())
        page=await self.post('search_matches',team_query='Alpha1')
        key=page.json()['search_key']
        result=await self.post('resolve_match',search_key=key,team_a='Alpha1',team_b='Beta1',
                               from_utc=self.now.isoformat())
        self.assertTrue(result.json()['resolved'])
        text='已唯一识别 Alpha1 对 Beta1，fixture leyu:1，无有效报价。'
        job=dict(id=self.job,attempt_id=self.attempt,message={})
        with patch.object(worker,'review_message',AsyncMock(return_value=(text,{'content':text})))as call:
            _,summary=await worker._review_until_verified(self.pool,SimpleNamespace(),job)
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(1,call.await_count)
        async with self.pool.acquire()as conn,conn.transaction():
            await trace(conn,self.job,'hermes_result',{**summary,'attempt_id':str(self.attempt),'content':text})
            await record_summary(conn,dict(id=self.job,message_id=self.message),self.attempt,'real_time')
        receipt=await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertTrue(receipt['completion_verified'])
        async with self.pool.acquire()as conn:
            explicit=await completion_issues(conn,self.job,self.attempt,{'leyu:2'},text)
        self.assertEqual(1,explicit['unbound_mention_count'])
        await self.pool.execute('UPDATE v2_review_fixture_bindings SET attempt_id=$2 WHERE job_id=$1',self.job,uuid.uuid4())
        self.assertEqual(2,(await self.issues(text))['unbound_mention_count'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_names_from_distinct_claims_cannot_form_an_extra_pair(self):
        await self.fixture(1,id='leyu:1',phase='finished',home_team='Crystal',away_team='Liverpool')
        await self.fixture(2,id='leyu:2',phase='finished',home_team='Ipswich',away_team='Bournemouth')
        await self.fixture(3,id='leyu:3',phase='finished',home_team='Bournemouth',away_team='Liverpool')
        page=await self.post('search_matches',team_query='',competition_query='League',sport='football',
                             from_utc=self.now.isoformat(),to_utc=(self.now+timedelta(hours=4)).isoformat())
        self.assertEqual(200,page.status_code,page.text)
        for home,away in(('Crystal','Liverpool'),('Ipswich','Bournemouth')):
            result=await self.post('resolve_match',search_key=page.json()['search_key'],team_a=home,team_b=away)
            self.assertTrue(result.json()['resolved'])
        self.assertEqual({},await self.issues('已唯一识别 Crystal 对 Liverpool，leyu:1。\n已唯一识别 Ipswich 对 Bournemouth，leyu:2。'))
        issues=await self.issues('已唯一识别 Crystal 对 Liverpool，leyu:1。已唯一识别 Bournemouth 对 Liverpool。')
        self.assertEqual(1,issues['unbound_mention_count'])
        self.assertEqual('leyu:3',issues['unbound_mentions'][0]['fixture_id'])
