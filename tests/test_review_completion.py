"""Completion requires current-attempt tool evidence, never model prose."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest
import uuid

from app import worker
from app.review_completion import completion_issues, mentioned_fixtures
from app.review_summary import record_summary
from app.review_state import trace
import test_reviews as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class ReviewCompletionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post

    async def prepare_search(self):
        for index in (1, 2):
            await support.ReviewTests.fixture(self, index=index, id=f'leyu:{index}', phase='finished')
        response = await self.post('search_matches', team_query='Alpha', page_size=1)
        self.assertEqual(200, response.status_code)
        return response.json()

    async def issues(self, mentioned):
        async with self.pool.acquire() as conn:
            return await completion_issues(conn, self.job, self.attempt, mentioned)

    async def test_unread_page_and_unbound_claim_cannot_count_as_verified(self):
        page = await self.prepare_search()
        issues = await self.issues({'leyu:1'})
        self.assertEqual(1, issues['unfinished_search_count'])
        self.assertEqual(page['next_cursor'], issues['unfinished_searches'][0]['cursor'])
        self.assertEqual(1, issues['unbound_mention_count'])
        self.assertEqual(page['search_key'], issues['unbound_mentions'][0]['candidate']['search_key'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))

    async def test_only_current_attempt_binding_clears_mention_after_full_paging(self):
        page = await self.prepare_search()
        await self.post('search_matches', team_query='Alpha', cursor=page['next_cursor'])
        await self.pool.execute(
            "INSERT INTO v2_review_fixture_bindings(job_id,attempt_id,fixture_id,search_key,evidence) VALUES($1,$2,'leyu:1',$3,'{}')",
            self.job, uuid.uuid4(), page['search_key'])
        self.assertEqual(1, (await self.issues({'leyu:1'}))['unbound_mention_count'])
        response = await self.post('resolve_match', search_key=page['search_key'], team_a='Alpha1', team_b='Beta1')
        self.assertTrue(response.json()['resolved'])
        self.assertEqual({}, await self.issues({'leyu:1'}))
        unknown = await self.issues({'leyu:999'})
        self.assertIsNone(unknown['unbound_mentions'][0]['candidate'])

    async def test_bounded_continuation_retains_dropped_mentions_and_partial_evidence(self):
        await self.prepare_search()
        job = {'id': self.job, 'attempt_id': self.attempt, 'message': {}, 'review_mode': 'real_time'}
        model = AsyncMock(side_effect=[('已全部匹配 leyu:1', {'content': '已全部匹配 leyu:1'}),
                                      ('已完成', {'content': '已完成'}), ('无遗漏', {'content': '无遗漏'})])
        with patch.object(worker, 'review_message', model):
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        self.assertEqual(3, model.await_count)
        self.assertFalse(summary['completion_verified'])
        self.assertEqual(1, summary['completion_issues']['unbound_mention_count'])
        self.assertEqual(2, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='hermes_partial_result'"))
        self.assertEqual(1, model.await_args_list[1].kwargs['continuation_round'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_continuation_closes_only_when_tools_finish_and_bind(self):
        page = await self.prepare_search()
        async def model(*args, **kwargs):
            if kwargs.get('continuation_round'):
                await self.post('search_matches', team_query='Alpha', cursor=page['next_cursor'])
                await self.post('resolve_match', search_key=page['search_key'], team_a='Alpha1', team_b='Beta1')
            return '匹配 leyu:1', {'content': '匹配 leyu:1'}
        job = {'id': self.job, 'attempt_id': self.attempt, 'message': {}, 'review_mode': 'real_time'}
        with patch.object(worker, 'review_message', AsyncMock(side_effect=model)) as call:
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        self.assertEqual(2, call.await_count)
        self.assertTrue(summary['completion_verified'])

    async def test_no_intent_has_no_extra_model_turn(self):
        job = {'id': self.job, 'attempt_id': self.attempt, 'message': {}, 'review_mode': 'real_time'}
        with patch.object(worker, 'review_message', AsyncMock(return_value=('聊天', {'content': '聊天'}))) as call:
            self.assertTrue((await worker._review_until_verified(self.pool, SimpleNamespace(), job))[1]['completion_verified'])
        self.assertEqual(1, call.await_count)

    async def test_final_receipt_rechecks_partial_turns_instead_of_trusting_final_success(self):
        await self.prepare_search()
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'hermes_partial_result',
                        {'attempt_id': str(self.attempt), 'content': '已全部匹配 leyu:1'})
            await trace(conn, self.job, 'hermes_result',
                        {'attempt_id': str(self.attempt), 'content': '全部完成', 'completion_verified': True})
            await record_summary(conn, {'id': self.job, 'message_id': self.message}, self.attempt, 'real_time')
        receipt = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertFalse(receipt['completion_verified'])
        self.assertEqual(1, receipt['completion_issues']['unbound_mention_count'])
        self.assertIn('不能认定全部匹配', receipt['content'])

    async def test_turn_costs_include_first_image_input_and_all_continuations(self):
        page = await self.prepare_search()
        async def model(*args, **kwargs):
            if kwargs.get('continuation_round'):
                await self.post('search_matches', team_query='Alpha', cursor=page['next_cursor'])
                return '完成', {'content': '完成', 'image_count': 0, 'provider_elapsed_ms': 20,
                                'usage': {'total_tokens': 30}}
            return '继续', {'content': '继续', 'image_count': 1, 'provider_elapsed_ms': 10,
                            'usage': {'total_tokens': 100}}
        job = {'id': self.job, 'attempt_id': self.attempt, 'message': {}}
        with patch.object(worker, 'review_message', AsyncMock(side_effect=model)):
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        self.assertEqual(1, summary['image_count'])
        self.assertEqual(30, summary['provider_elapsed_ms'])
        self.assertEqual(130, summary['usage']['total_tokens'])

    async def test_expired_lease_stops_continuation(self):
        job = {'id': self.job, 'attempt_id': self.attempt, 'message': {}}
        await self.prepare_search()
        async def expired(*args, **kwargs):
            await self.pool.execute("UPDATE v2_analysis_jobs SET lease_until=now()-interval '1 second' WHERE id=$1", self.job)
            return '完成', {'content': '完成'}
        with patch.object(worker, 'review_message', AsyncMock(side_effect=expired)) as call:
            with self.assertRaises(Exception) as raised:
                await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual(1, call.await_count)


class MentionTests(unittest.TestCase):
    def test_only_bounded_canonical_fixture_tokens_are_used_as_verification_hints(self):
        self.assertEqual({'leyu:123', 'leyu:456'}, mentioned_fixtures('leyu:123、leyu:456 and leyu:123'))
