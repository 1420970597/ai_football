"""An agent's claim that it has no tools cannot silently finish a review."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from app import worker
from app.review_state import trace
from app.review_summary import record_summary
import test_reviews as support


CLAIM = ('我不会使用足球 MCP 工具，也不会使用终端或文件工具：本次会话没有提供这些工具，'
         '因此我无法搜索比赛、读取市场盘口，也无法记录任何内容。')
NATIVE_ABSENCE_CLAIM = (
    '我不能审核这条消息：本轮所需的工具尚未接入，当前没有可用的 football / football_parallel MCP 工具'
    '（我这边只有技能读取和通用文件、命令行工具），无法检索比赛、读取盘口或录入推荐。')
CONTEXT_ABSENCE_CLAIM = '我不会审核这条消息，因为当前提供的上下文里没有可用的比赛或盘口工具。'


# 生产样本 a17d1748：三项 can_record=true 的推荐已核验，但最后一次提交只被写成了正文。
TAG = '<' + '/parameter>'
LEAKED_CALL = ('Batch A: football: Egypt; parallel: Eibar. Then Alavés.\n\nLet me do batch A.' + TAG)


class UnexecutedToolCallBoundaryTests(unittest.TestCase):
    def test_prospective_tool_prose_is_not_a_completed_review(self):
        from app.review_completion import unexecuted_tool_call_claim
        for content in ('Let me analyze the source.\n\nLet me call.',
                        'Let me make the call.', 'Let me do the search.',
                        'I will now call search_matches.', '现在调用比赛检索。'):
            with self.subTest(content=content):
                self.assertTrue(unexecuted_tool_call_claim(content))

    def test_reported_or_completed_actions_do_not_request_continuation(self):
        from app.review_completion import unexecuted_tool_call_claim
        for content in ('I called search_matches and no fixture matched.',
                        '> Let me call.', '原文引用：Let me call.',
                        'Let me call.\n本条是赛果反馈，没有新增推荐。',
                        'The source says “Let me call.”', 'Let me analyze this result.'):
            with self.subTest(content=content):
                self.assertFalse(unexecuted_tool_call_claim(content))

    def test_trailing_serialized_call_is_detected(self):
        from app.review_completion import unexecuted_tool_call_claim
        for content in (LEAKED_CALL, LEAKED_CALL + '\n', LEAKED_CALL + '\n<' + '/invoke>\n<' + '/function_calls>  ',
                        '现在提交。<' + '/tool_call>', 'x' * 30000 + TAG):
            with self.subTest(content=content[-40:]):
                self.assertTrue(unexecuted_tool_call_claim(content))

    def test_conclusions_quotes_and_mid_text_markup_are_not_detected(self):
        from app.review_completion import unexecuted_tool_call_claim
        for content in ('', '本条是赛果反馈，没有新增推荐。', '已接受 1 条推荐：全场大2.5。',
                        '原文包含标记 ' + TAG + '，但与审核无关，本条无推荐。',
                        LEAKED_CALL + '\n\n已提交并接受 2 条推荐。', '参数说明：parameter>', '比分 2>1，已完场。'):
            with self.subTest(content=content[-40:]):
                self.assertFalse(unexecuted_tool_call_claim(content))


class ToolClaimBoundaryTests(unittest.TestCase):
    def test_explicit_self_limitations_are_only_negative_completion_hints(self):
        from app.review_completion import tool_unavailability_claim
        for content in (CLAIM, NATIVE_ABSENCE_CLAIM, CONTEXT_ABSENCE_CLAIM,
                        '我无法处理本条消息：本次上下文中未提供可用的赛事与盘口工具。',
                        '当前会话没有提供足球MCP工具，无法执行核验。',
                        '我无法使用football MCP工具。',
                        'I cannot use football MCP tools because this session does not provide any tools.',
                        "I don't have access to football MCP tools.", 'No football MCP tools are available.'):
            with self.subTest(content=content):
                self.assertTrue(tool_unavailability_claim(content))

    def test_quotes_no_intent_and_no_match_are_not_tool_failures(self):
        from app.review_completion import tool_unavailability_claim
        for content in ('原文引用：' + CLAIM, '> ' + CLAIM, '我引用原文：' + CLAIM,
                        'I quote: ' + CLAIM, '本次完整检索没有匹配比赛。',
                        '我不能断言当前没有可用的 football MCP 工具，实际调用已成功。',
                        '我不能审核原文引用：“当前没有可用的 football MCP 工具”。',
                        '我不会审核这条消息，假如当前上下文没有比赛或盘口工具。',
                        '我没有调用足球MCP工具，因为本条是表情包，无推荐意图。',
                        '本条没有推荐意图，无需调用足球MCP工具。'):
            with self.subTest(content=content):
                self.assertFalse(tool_unavailability_claim(content))

    def test_later_paragraph_and_oversized_suffix_do_not_trigger_first_person_claims(self):
        from app.review_completion import tool_unavailability_claim
        self.assertFalse(tool_unavailability_claim('本条无推荐。\n\n原文引用：' + CLAIM))
        self.assertFalse(tool_unavailability_claim('本条无推荐。' + '。' * 2000 + CLAIM))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class ReviewToolAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post

    async def run_model(self, side_effect):
        # The support fixture uses self.job for its UUID and HTTP context.
        job = {'id': self.job, 'attempt_id': self.attempt, 'message': {}, 'review_mode': 'real_time'}
        with patch.object(worker, 'review_message', AsyncMock(side_effect=side_effect)) as model:
            result = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        return result, model

    async def test_claim_then_only_changed_words_cannot_finish(self):
        model = AsyncMock(side_effect=[(CLAIM, {'content': CLAIM}), ('已经完成', {'content': '已经完成'}),
                                      ('无遗漏', {'content': '无遗漏'})])
        job = {'id': self.job, 'attempt_id': self.attempt, 'message': {}, 'review_mode': 'real_time'}
        with patch.object(worker, 'review_message', model):
            with self.assertRaises(worker.HermesProviderUnavailable):
                await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        self.assertEqual(3, model.await_count)
        self.assertEqual(3, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='hermes_partial_result'"))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_real_current_attempt_tool_after_claim_restores_completion(self):
        calls = []

        async def model(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return CLAIM, {'content': CLAIM}
            response = await self.post('search_matches', team_query='不存在的隔离球队')
            self.assertEqual(200, response.status_code, response.text)
            self.assertTrue(response.json()['search_complete'])
            return '工具已完整检索，没有合理候选，保留未解决。', {'content': '无合理候选'}

        (_, summary), _ = await self.run_model(model)
        self.assertEqual(2, len(calls))
        self.assertEqual(1, calls[1]['continuation_round'])
        self.assertIn('football', calls[1]['continuation'])
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_old_wrong_attempt_and_failed_tool_calls_do_not_restore_completion(self):
        await trace(self.pool, self.job, 'mcp_search_matches',
                    {'attempt_id': str(self.attempt), 'status': 'succeeded'})
        calls = []

        async def model(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return CLAIM, {'content': CLAIM}
            await trace(self.pool, self.job, 'mcp_search_matches',
                        {'attempt_id': str(uuid.uuid4()), 'status': 'succeeded'})
            await trace(self.pool, self.job, 'mcp_search_matches',
                        {'attempt_id': str(self.attempt), 'status': 'failed'})
            return '已经完成', {'content': '已经完成'}

        with self.assertRaises(worker.HermesProviderUnavailable):
            await self.run_model(model)
        self.assertEqual(3, len(calls))

    async def test_unexecuted_tool_call_continues_until_a_real_conclusion(self):
        final = '已提交并核对工具回执，本条没有可录入的推荐。'
        (content, summary), model = await self.run_model([(LEAKED_CALL, {'content': LEAKED_CALL}),
                                                         (final, {'content': final})])
        self.assertEqual(final, content)
        self.assertEqual(2, model.await_count)
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(2, summary['completion_rounds'])
        prompt = model.await_args_list[1].kwargs['continuation']
        self.assertIn('未执行的工具调用', prompt)
        self.assertIn('不是新的绑定、盘口或录入许可', prompt)
        self.assertEqual(1, model.await_args_list[1].kwargs['continuation_round'])
        partial = await self.pool.fetchval(
            "SELECT payload FROM v2_analysis_traces WHERE trace_kind='hermes_partial_result'")
        self.assertTrue(partial['completion_issues']['tool_call_unexecuted'])
        self.assertFalse(partial['completion_verified'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_repeated_unexecuted_tool_call_ends_unverified_without_inventing_signals(self):
        (_, summary), model = await self.run_model([(LEAKED_CALL, {'content': LEAKED_CALL})] * 3)
        self.assertEqual(3, model.await_count)
        self.assertFalse(summary['completion_verified'])
        self.assertTrue(summary['completion_issues']['tool_call_unexecuted'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_historical_replay_does_not_continue_for_serialized_call_text(self):
        job = {'id': self.job, 'attempt_id': self.attempt, 'message': {}, 'review_mode': 'historical_replay'}
        with patch.object(worker, 'review_message', AsyncMock(return_value=(LEAKED_CALL, {'content': LEAKED_CALL}))) as model:
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        self.assertEqual(1, model.await_count)
        self.assertTrue(summary['completion_verified'])

    async def test_final_receipt_marks_unexecuted_tool_call_even_if_worker_flag_is_true(self):
        receipt = await self.receipt(claim=LEAKED_CALL)
        self.assertFalse(receipt['completion_verified'])
        self.assertTrue(receipt['completion_issues']['tool_call_unexecuted'])
        self.assertNotIn('tool_availability_unverified', receipt['completion_issues'])
        self.assertIn('未执行的工具调用', receipt['content'])
        self.assertEqual(0, receipt['accepted_count'])

    async def test_english_explicit_tool_absence_also_requires_recovery(self):
        claim = 'I cannot use football MCP tools because this session does not provide any tools.'
        with self.assertRaises(worker.HermesProviderUnavailable):
            await self.run_model([(claim, {'content': claim})] * 3)

    async def test_native_no_available_tools_claim_cannot_silently_finish(self):
        with self.assertRaises(worker.HermesProviderUnavailable):
            await self.run_model([(NATIVE_ABSENCE_CLAIM, {'content': NATIVE_ABSENCE_CLAIM})] * 3)
        self.assertEqual(3, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='hermes_partial_result'"))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_current_context_absence_requires_actual_tool_recovery(self):
        with self.assertRaises(worker.HermesProviderUnavailable):
            await self.run_model([(CONTEXT_ABSENCE_CLAIM, {'content': CONTEXT_ABSENCE_CLAIM})] * 3)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_quoted_tool_absence_and_ordinary_no_intent_keep_one_turn(self):
        for content in ('原文引用：' + CLAIM, '> ' + CLAIM,
                        '本条是熊猫表情包，没有推荐意图，无需调用工具。',
                        '工具已完成检索，没有匹配的比赛。'):
            with self.subTest(content=content):
                (_, summary), model = await self.run_model([(content, {'content': content})])
                self.assertEqual(1, model.await_count)
                self.assertTrue(summary['completion_verified'])

    async def test_historical_archive_does_not_require_current_market_tool_calls(self):
        job = {'id': self.job, 'attempt_id': self.attempt, 'message': {}, 'review_mode': 'historical_replay'}
        with patch.object(worker, 'review_message', AsyncMock(return_value=(CLAIM, {'content': CLAIM}))) as model:
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        self.assertEqual(1, model.await_count)
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_match_searches'))
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'hermes_result',
                        {'attempt_id': str(self.attempt), 'content': CLAIM})
            await record_summary(conn, {'id': self.job, 'message_id': self.message}, self.attempt, 'historical_replay')
        receipt = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertTrue(receipt['completion_verified'])
        self.assertIn('历史回放', receipt['content'])

    async def receipt(self, *, partial=False, real_tool=False, claim=CLAIM):
        if partial:
            await trace(self.pool, self.job, 'mcp_search_matches',
                        {'attempt_id': str(self.attempt), 'status': 'succeeded'})
            await trace(self.pool, self.job, 'hermes_partial_result',
                        {'attempt_id': str(self.attempt), 'content': CLAIM,
                         'completion_issues': {'tool_availability_unverified': True}})
        if real_tool:
            response = await self.post('search_matches', team_query='不存在的隔离球队')
            self.assertEqual(200, response.status_code, response.text)
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'hermes_result',
                        {'attempt_id': str(self.attempt), 'content': '已经完成' if partial else claim,
                         'completion_verified': True})
            await record_summary(conn, {'id': self.job, 'message_id': self.message}, self.attempt, 'real_time')
        return await self.pool.fetchval(
            "SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result' ORDER BY sequence_no DESC LIMIT 1")

    async def test_final_receipt_rejects_explicit_claim_even_if_worker_flag_is_true(self):
        receipt = await self.receipt()
        self.assertFalse(receipt['completion_verified'])
        self.assertTrue(receipt['completion_issues']['tool_availability_unverified'])
        self.assertIn('工具', receipt['content'])

    async def test_final_receipt_rejects_native_no_available_tools_claim(self):
        receipt = await self.receipt(claim=NATIVE_ABSENCE_CLAIM)
        self.assertFalse(receipt['completion_verified'])
        self.assertTrue(receipt['completion_issues']['tool_availability_unverified'])

    async def test_final_receipt_rejects_current_context_absence_claim(self):
        receipt = await self.receipt(claim=CONTEXT_ABSENCE_CLAIM)
        self.assertFalse(receipt['completion_verified'])
        self.assertTrue(receipt['completion_issues']['tool_availability_unverified'])

    async def test_final_receipt_requires_tool_after_claim_not_before_it(self):
        receipt = await self.receipt(partial=True)
        self.assertFalse(receipt['completion_verified'])
        self.assertTrue(receipt['completion_issues']['tool_availability_unverified'])

    async def test_final_receipt_accepts_real_tool_after_partial_without_inventing_signals(self):
        receipt = await self.receipt(partial=True, real_tool=True)
        self.assertTrue(receipt['completion_verified'])
        self.assertEqual(0, receipt['accepted_count'])
