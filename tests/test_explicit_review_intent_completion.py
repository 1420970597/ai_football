"""Plain source instructions cannot disappear as an unverified no-intent answer."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest
from uuid import uuid4

from app import review_completion as checks, worker
from app.review_state import trace
from app.review_summary import record_summary
import test_reviews as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires task-owned isolated database')
class ExplicitReviewIntentTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def test_only_short_whole_directives_request_verification(self):
        for text in ('追 4.5/5 大', '跟小2.5', '上 3 大球！'):
            self.assertTrue(checks.explicit_text_pick(text))
        for text in ('昨天追4.5/5大', '收米，之前追4.5/5大', '4.5/5大', '追4.5/5大取消',
                     '聊天', '追' + '1' * 200 + '大'):
            self.assertFalse(checks.explicit_text_pick(text))

    async def test_unverified_no_intent_stops_at_existing_budget_without_signal(self):
        job = dict(id=self.job, attempt_id=self.attempt, message={'raw_text': '追 4.5/5 大'})
        call = AsyncMock(return_value=('没有新增推荐，图片未高亮', {'content': '没有新增推荐，图片未高亮'}))
        with patch.object(worker, 'review_message', call):
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        self.assertEqual(3, call.await_count)
        self.assertFalse(summary['completion_verified'])
        self.assertTrue(summary['completion_issues']['text_intent_unverified'])
        self.assertIn('不要求', call.await_args_list[1].kwargs['continuation'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_continuation_stops_only_after_real_finished_identity_binding(self):
        await self.fixture(1, id='leyu:1', phase='finished')
        async def answer(*args, **kwargs):
            if kwargs.get('continuation_round'):
                response = await self.post('search_matches', team_query='Alpha', team_a='Alpha1', team_b='Beta1')
                self.assertTrue(response.json()['resolution']['resolved'])
            return '没有新增推荐', {'content': '没有新增推荐'}
        job = dict(id=self.job, attempt_id=self.attempt, message={'raw_text': '追 4.5/5 大'})
        with patch.object(worker, 'review_message', AsyncMock(side_effect=answer)) as call:
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        self.assertEqual(2, call.await_count)
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_feedback_and_chat_do_not_add_model_turns(self):
        for text in ('昨天追4.5/5大', '收米', '聊天'):
            with self.subTest(text=text):
                job = dict(id=self.job, attempt_id=self.attempt, message={'raw_text': text})
                with patch.object(worker, 'review_message', AsyncMock(return_value=('无新增推荐', {}))) as call:
                    summary = (await worker._review_until_verified(self.pool, SimpleNamespace(), job))[1]
                self.assertEqual(1, call.await_count)
                self.assertTrue(summary['completion_verified'])

    async def test_formatted_source_pick_cannot_finish_without_identity_tools(self):
        for text in ('国际友谊赛\n甲队VS乙队\n足球（滚球）大/小 全场大2.5\n正反随意 红黑勿怪',
                     '09:00 测试联赛\n甲队VS乙队\n足球（初盘）让球 客-0.25'):
            with self.subTest(text=text):
                self.assertTrue(checks.explicit_text_pick(text))
                job = dict(id=self.job, attempt_id=self.attempt, message={'raw_text': text})
                model = AsyncMock(return_value=('当前没有新增推荐。', {}))
                with patch.object(worker, 'review_message', model):
                    _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
                self.assertEqual(3, model.await_count)
                self.assertFalse(summary['completion_verified'])
                self.assertTrue(summary['completion_issues']['text_intent_unverified'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_form_rows_in_feedback_quotes_and_questions_are_not_current(self):
        for text in ('赛果反馈\n足球（滚球）大/小 全场大2.5',
                     '> 足球（初盘）让球 客-0.25',
                     '足球（滚球）大/小 全场大2.5？', '足球（滚球）大/小 全场大2.5 收米',
                     '足球（初盘）让球 客-0.25 取消', '赔率1.75\n比分0-0'):
            with self.subTest(text=text):
                self.assertFalse(checks.explicit_text_pick(text))

    async def test_final_receipt_does_not_trust_plain_directive_no_intent_prose(self):
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2 WHERE id=$1', self.message, '追 4.5/5 大')
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'hermes_result', dict(attempt_id=str(self.attempt), content='没有新增推荐'))
            await record_summary(conn, dict(id=self.job, message_id=self.message), self.attempt, 'real_time')
        receipt = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertFalse(receipt['completion_verified'])
        self.assertTrue(receipt['completion_issues']['text_intent_unverified'])

    async def test_explicit_recommendations_and_named_half_total_request_verification(self):
        for text in (
            '推荐：蔚山现代太阳神+1.5分  （重注推荐）\n推荐：神户鹳鸟+11.5分\n推荐：全场172.5大',
            '国际友谊  16:00  22\n卡塔尔 U23  0-0  新加坡 U23\n半场0.75大',
            '昨日战绩：甲队输了\n今日推荐：乙队+1.5分',
            '半大 0.75', '推薦：乙隊受讓1/1.5', '推荐：全场小172.5',
        ):
            with self.subTest(text=text):
                self.assertTrue(checks.explicit_text_pick(text))
        for text in (
            '昨日战绩\n推荐：蔚山现代太阳神+1.5分 收米',
            '昨天推荐：神户鹳鸟+11.5分',
            '推荐：充值500送150', '上半场大0.75可以吗？',
            '赛果反馈\n半场0.75大', '赔率1.75\n比分0-0',
            '推荐：赔率+1.75', '推荐：乙队+1.3分',
            '> 推荐：乙队+1.5分', '```\n推荐：乙队+1.5分\n```',
            '推荐：全场大172.5？', '今日推荐：乙队+1.5分 取消',
        ):
            with self.subTest(text=text):
                self.assertFalse(checks.explicit_text_pick(text))

    async def test_multi_recommendation_no_intent_answer_is_not_verified(self):
        text = ('推荐：蔚山现代太阳神+1.5分  （重注推荐）\n推荐：神户鹳鸟+11.5分\n推荐：全场172.5大\n'
                + '继续分享篮球赛事，跟随与否自行决定。' * 5)
        self.assertGreater(len(text), 100)
        job = dict(id=self.job, attempt_id=self.attempt, message={'raw_text': text})
        call = AsyncMock(return_value=('没有新增推荐', {'content': '没有新增推荐'}))
        with patch.object(worker, 'review_message', call):
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        self.assertEqual(3, call.await_count)
        self.assertFalse(summary['completion_verified'])
        self.assertTrue(summary['completion_issues']['text_intent_unverified'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_final_receipt_keeps_long_explicit_recommendation_unverified(self):
        text = '推荐：蔚山现代太阳神+1.5分\n' + '推荐后的普通说明。' * 20
        self.assertGreater(len(text), 100)
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2 WHERE id=$1', self.message, text)
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'hermes_result', dict(attempt_id=str(self.attempt), content='没有新增推荐'))
            await record_summary(conn, dict(id=self.job, message_id=self.message), self.attempt, 'real_time')
        receipt = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertFalse(receipt['completion_verified'])
        self.assertTrue(receipt['completion_issues']['text_intent_unverified'])
        self.assertIn('推荐或追线指令', receipt['content'])

    async def test_other_attempt_binding_cannot_clear_explicit_recommendation(self):
        await self.fixture(1, id='leyu:1', phase='finished')
        response = await self.post('search_matches', team_query='Alpha', team_a='Alpha1', team_b='Beta1')
        self.assertTrue(response.json()['resolution']['resolved'])
        other_attempt = uuid4()
        issues = await checks.completion_issues(
            self.pool, self.job, other_attempt, set(), require_intent_review=True)
        self.assertTrue(issues['text_intent_unverified'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
