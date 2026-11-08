"""Named identity claims need the same current tool proof as fixture IDs."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest
import uuid

from app import worker
from app.review_completion import completion_issues, named_identity_claims
from app.review_state import trace
from app.review_summary import record_summary
import test_reviews as support


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
