"""Plain source instructions cannot disappear as an unverified no-intent answer."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

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

    async def test_final_receipt_does_not_trust_plain_directive_no_intent_prose(self):
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2 WHERE id=$1', self.message, '追 4.5/5 大')
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'hermes_result', dict(attempt_id=str(self.attempt), content='没有新增推荐'))
            await record_summary(conn, dict(id=self.job, message_id=self.message), self.attempt, 'real_time')
        receipt = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertFalse(receipt['completion_verified'])
        self.assertTrue(receipt['completion_issues']['text_intent_unverified'])
