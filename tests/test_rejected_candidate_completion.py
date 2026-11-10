import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app import worker
from app.review_completion import mentioned_fixtures
from app.review_state import trace
from app.review_summary import record_summary
import test_reviews as support
import test_review_completion as completion_support


class RejectedCandidateMentionTests(unittest.TestCase):
    def test_source_kickoff_excludes_single_same_name_candidate_without_a_pick_claim(self):
        text=('两点说明：同组20:00还有一场13:20Z开球的越南U23 vs 泰国U23滚球盘'
              '（leyu:98886201，已1-0进行中），按截图开球时间20:00排除，绑定的是尚未开赛那场。'
              '越南U23 vs 泰国U23已唯一绑定 leyu:98886008。')
        self.assertEqual({'leyu:98886008'},mentioned_fixtures(text))

    def test_time_exclusion_needs_distinct_times_and_explicit_source_basis(self):
        for text in ('leyu:1按截图开球时间20:00排除',
                     'leyu:1开球20:00，按截图开球时间20:00排除',
                     'leyu:1开球13:20，按库存时间20:00排除',
                     'leyu:1开球13:20，按截图开球时间20:00尚不能排除',
                     'leyu:1和leyu:2开球13:20，按截图开球时间20:00排除'):
            with self.subTest(text=text):
                self.assertIn('leyu:1',mentioned_fixtures(text))

    def test_time_exclusion_does_not_erase_a_positive_claim_in_the_same_or_prior_clause(self):
        for text in ('已唯一绑定leyu:1，开球13:20，按截图开球时间20:00排除',
                     '已唯一绑定leyu:1。候选leyu:1开球13:20，按截图开球时间20:00排除'):
            with self.subTest(text=text):
                self.assertEqual({'leyu:1'},mentioned_fixtures(text))

    def test_explicit_irrelevant_candidate_is_not_a_positive_fixture_claim(self):
        content=('唯一未绑定候选仍是EAFC24电子赛（leyu:2119387627093266434，完场），'
                 '与原文英超正赛的开球时间、赛制均不符，不作为绑定，也不读其盘口。\n'
                 '水晶宫对利物浦已唯一绑定 leyu:5803628。\n'
                 '伊普斯维奇对伯恩茅斯已唯一绑定 leyu:5803626。')
        self.assertEqual({'leyu:5803626','leyu:5803628'},mentioned_fixtures(content))

    def test_uncertainty_prices_closed_games_and_mixed_positive_claims_still_require_proof(self):
        for text in ('未绑定候选 leyu:1', 'leyu:1没有报价，不作为绑定',
                     'leyu:1已完场，不作为绑定', 'leyu:1尚不能确认是否与原文不符',
                     '已唯一绑定 leyu:1，但另一候选 leyu:2 与原文不符，不作为绑定'):
            with self.subTest(text=text):
                self.assertIn('leyu:1',mentioned_fixtures(text))
        self.assertEqual({'leyu:1','leyu:2'},mentioned_fixtures(
            '已唯一绑定 leyu:1，但另一候选 leyu:2 与原文不符，不作为绑定'))
        self.assertEqual({'leyu:1'},mentioned_fixtures('已唯一绑定leyu:1。'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires task-owned isolated database')
class RejectedCandidateCompletionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    prepare_search=completion_support.ReviewCompletionTests.prepare_search

    async def test_time_excluded_alternate_does_not_force_a_binding_after_complete_search(self):
        page=await self.prepare_search()
        await self.post('search_matches',team_query='Alpha',cursor=page['next_cursor'])
        self.assertTrue((await self.post('resolve_match',search_key=page['search_key'],
                                       team_a='Alpha1',team_b='Beta1')).json()['resolved'])
        text='已唯一绑定leyu:1。候选leyu:2开球13:20，按截图开球时间20:00排除。'
        job={'id':self.job,'attempt_id':self.attempt,'message':{}}
        with patch.object(worker,'review_message',AsyncMock(return_value=(text,{'content':text})))as call:
            _,summary=await worker._review_until_verified(self.pool,SimpleNamespace(),job)
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(1,call.await_count)
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_time_exclusion_cannot_erase_an_earlier_positive_claim_or_unread_pages(self):
        await self.prepare_search()
        texts=['已唯一绑定leyu:2。','候选leyu:2开球13:20，按截图开球时间20:00排除。','完成']
        job={'id':self.job,'attempt_id':self.attempt,'message':{}}
        with patch.object(worker,'review_message',AsyncMock(side_effect=[(t,{'content':t})for t in texts]))as call:
            _,summary=await worker._review_until_verified(self.pool,SimpleNamespace(),job)
        self.assertFalse(summary['completion_verified'])
        self.assertEqual(1,summary['completion_issues']['unfinished_search_count'])
        self.assertEqual(1,summary['completion_issues']['unbound_mention_count'])
        self.assertEqual(3,call.await_count)
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))

    async def test_complete_search_and_real_binding_finish_once_and_receipt_uses_same_claims(self):
        page=await self.prepare_search()
        await self.post('search_matches',team_query='Alpha',cursor=page['next_cursor'])
        result=await self.post('resolve_match',search_key=page['search_key'],team_a='Alpha1',team_b='Beta1')
        self.assertTrue(result.json()['resolved'])
        text='已唯一绑定 leyu:1。候选 leyu:2 与原文比赛不符，不作为绑定。'
        job={'id':self.job,'attempt_id':self.attempt,'message':{},'review_mode':'real_time'}
        with patch.object(worker,'review_message',AsyncMock(return_value=(text,{'content':text})))as call:
            _,summary=await worker._review_until_verified(self.pool,SimpleNamespace(),job)
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(1,call.await_count)
        async with self.pool.acquire()as conn,conn.transaction():
            await trace(conn,self.job,'hermes_result',{'attempt_id':str(self.attempt),**summary})
            await record_summary(conn,{'id':self.job,'message_id':self.message},self.attempt,'real_time')
        receipt=await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertTrue(receipt['completion_verified'])
        self.assertEqual(0,receipt['accepted_count'])
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_rejected_candidate_cannot_clear_unread_search_pages(self):
        await self.prepare_search()
        text='候选 leyu:2 与原文赛制不符，不作为绑定。'
        job={'id':self.job,'attempt_id':self.attempt,'message':{}}
        with patch.object(worker,'review_message',AsyncMock(return_value=(text,{'content':text})))as call:
            _,summary=await worker._review_until_verified(self.pool,SimpleNamespace(),job)
        self.assertFalse(summary['completion_verified'])
        self.assertEqual(1,summary['completion_issues']['unfinished_search_count'])
        self.assertEqual(3,call.await_count)

    async def test_later_exclusion_cannot_erase_prior_positive_unbound_claim(self):
        page=await self.prepare_search()
        await self.post('search_matches',team_query='Alpha',cursor=page['next_cursor'])
        texts=['已唯一绑定 leyu:2。','候选 leyu:2 与原文比赛不符，不作为绑定。','完成']
        job={'id':self.job,'attempt_id':self.attempt,'message':{}}
        with patch.object(worker,'review_message',AsyncMock(side_effect=[(t,{'content':t})for t in texts]))as call:
            _,summary=await worker._review_until_verified(self.pool,SimpleNamespace(),job)
        self.assertFalse(summary['completion_verified'])
        self.assertEqual(1,summary['completion_issues']['unbound_mention_count'])
        self.assertEqual(3,call.await_count)
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
