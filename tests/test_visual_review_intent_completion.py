"""Explicit image captions cannot be discarded as no recommendation intent."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

from app import review_completion as checks, worker
from app.review_state import trace
from app.review_summary import record_summary
import test_review_phase_completion as phases
from test_review_summary_evidence import CountedConnection


BAD = ('注单截图显示的是已成交注单，正文也没有新的跟进指令，'
       '所以这里不把它当作新增推荐来记录。')
GOOD = '原文可单可串明确推荐意图；图片两项仍需核验，没有有效报价，保留未解决。'
IMAGE = {'mime_type': 'image/png', 'object_key': 'isolated-image-only'}
CAPTION = '英超 22:00\n英超 22:00\n\n2x1 可單可串'


class VisualInstructionClaimTests(unittest.TestCase):
    def instruction(self, text, has_image=True):
        return getattr(checks, 'explicit_visual_instruction', lambda *args: False)(text, has_image)

    def denial(self, text):
        return getattr(checks, 'source_intent_denial', lambda *args: False)(text)

    def test_original_caption_and_both_scripts(self):
        for text in (CAPTION, '2x1 可單可串', '可单可串', '２×１\n可單可串！'):
            with self.subTest(text=text):
                self.assertTrue(self.instruction(text))

    def test_no_image_does_not_authorize_visual_check(self):
        self.assertFalse(self.instruction('可单可串', False))

    def test_quotes_history_feedback_and_long_messages_are_not_explicit_captions(self):
        for text in ('> 可单可串', '```\n可单可串\n```', '昨天可单可串',
                     '之前2x1可单可串，收米', '可单可串取消', '可单可串' + ' ' * 200):
            with self.subTest(text=text):
                self.assertFalse(self.instruction(text))

    def test_only_bounded_time_headers_can_precede_the_instruction(self):
        for text in ('昨天英超 22:00\n可单可串', '英超 25:99\n可单可串',
                     '> 英超 22:00\n可单可串', '收米\n可单可串',
                     '联赛 22:00\n' * 5 + '可单可串'):
            with self.subTest(text=text):
                self.assertFalse(self.instruction(text))

    def test_original_model_denial_is_detected(self):
        self.assertTrue(self.denial(BAD))

    def test_explicit_intent_denials_are_detected(self):
        for text in ('没有新增推荐', '这条没有新增推荐意图。', '本条无新增推荐。',
                     '这条不构成新增推荐。', '正文没有新的跟进指令。'):
            with self.subTest(text=text):
                self.assertTrue(self.denial(text))

    def test_no_quote_is_not_no_intent(self):
        for text in (GOOD, '有推荐意图，但没有新增推荐信号。',
                     '当前没有新增推荐可录入，因为无有效报价。',
                     '未接受任何信号，未新增记录，保留未解决。'):
            with self.subTest(text=text):
                self.assertFalse(self.denial(text))

    def test_quotes_fences_and_previous_error_reports_are_not_current_denials(self):
        for text in ('> 没有新增推荐', '```\n没有新增推荐\n```',
                     '上轮说明：没有新增推荐。', '原文引用：没有新增推荐。',
                     '错误说法为“没有新增推荐”，实际需继续核验。'):
            with self.subTest(text=text):
                self.assertFalse(self.denial(text))

    def test_correct_rejection_of_wrong_policy_is_not_a_denial(self):
        for text in ('不能因为已成交就说没有新增推荐。', '不应认定没有新增推荐。',
                     '没有新增推荐的说法是错误的，原文可单可串仍需核验。'):
            with self.subTest(text=text):
                self.assertFalse(self.denial(text))

    def test_negated_denial_is_not_a_current_denial(self):
        for text in ('并非没有新增推荐，只是没有有效报价。', '不是没有新增推荐。',
                     '注单确认不代表没有新增推荐。', '无报价不等于没有新增推荐。'):
            with self.subTest(text=text):
                self.assertFalse(self.denial(text))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires task-owned isolated database')
class VisualReviewIntentTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = phases.ReviewPhaseCompletionTests.asyncSetUp
    asyncTearDown = phases.ReviewPhaseCompletionTests.asyncTearDown
    context = phases.ReviewPhaseCompletionTests.context
    post = phases.ReviewPhaseCompletionTests.post
    fixture = phases.ReviewPhaseCompletionTests.fixture
    prepare = phases.ReviewPhaseCompletionTests.prepare

    async def review(self, texts, *, media=None, caption=CAPTION, mode='real_time'):
        job = dict(id=self.job, attempt_id=self.attempt, review_mode=mode,
                   message={'raw_text': caption, 'media_objects': [IMAGE] if media is None else media})
        call = AsyncMock(side_effect=[(text, {'content': text}) for text in texts])
        with patch.object(worker, 'review_message', call):
            result = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        return result[1], call

    async def test_existing_binding_does_not_hide_denied_caption_intent(self):
        await self.prepare()
        summary, call = await self.review([BAD] * 3)
        self.assertEqual(3, call.await_count)
        self.assertFalse(summary['completion_verified'])
        self.assertTrue(summary['completion_issues']['source_visual_intent_conflict'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_corrected_intent_needs_only_one_continuation_without_new_signal(self):
        await self.prepare()
        summary, call = await self.review([BAD, GOOD])
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(2, call.await_count)
        self.assertIn('可单可串', call.await_args_list[1].kwargs['continuation'])
        self.assertIn('不重复', call.await_args_list[1].kwargs['continuation'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_correct_no_quote_answer_has_no_extra_turn(self):
        await self.prepare()
        summary, call = await self.review([GOOD])
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(1, call.await_count)

    async def test_no_image_feedback_and_history_do_not_add_model_turns(self):
        for options in ({'media': []}, {'media': [{'mime_type': 'application/pdf'}]},
                        {'caption': '昨天可单可串，收米'}, {'mode': 'historical_replay'}):
            with self.subTest(options=options):
                summary, call = await self.review([BAD], **options)
                self.assertTrue(summary['completion_verified'])
                self.assertEqual(1, call.await_count)

    async def receipt(self, text, *, media=None):
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2,media_objects=$3 WHERE id=$1',
                                self.message, CAPTION, [IMAGE] if media is None else media)
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'hermes_result', dict(attempt_id=str(self.attempt), content=text,
                                                            completion_verified=True))
            await record_summary(conn, dict(id=self.job, message_id=self.message), self.attempt, 'real_time')
        return await self.pool.fetchval("SELECT payload FROM v2_analysis_traces "
                                       "WHERE trace_kind='system_review_result' ORDER BY sequence_no DESC LIMIT 1")

    async def test_final_transaction_rejects_false_model_success_even_after_binding(self):
        await self.prepare()
        receipt = await self.receipt(BAD)
        self.assertFalse(receipt['completion_verified'])
        self.assertTrue(receipt['completion_issues']['source_visual_intent_conflict'])
        self.assertIn('可单可串', receipt['content'])
        self.assertEqual(0, receipt['accepted_count'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_final_transaction_accepts_correct_zero_quote_explanation(self):
        await self.prepare()
        receipt = await self.receipt(GOOD)
        self.assertTrue(receipt['completion_verified'])
        self.assertEqual(0, receipt['accepted_count'])

    async def test_final_transaction_does_not_invent_image_from_caption(self):
        receipt = await self.receipt(BAD, media=[])
        self.assertTrue(receipt['completion_verified'])

    async def test_correct_caption_receipt_keeps_single_remote_evidence_read(self):
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2,media_objects=$3 WHERE id=$1',
                                self.message, CAPTION, [IMAGE])
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'hermes_result', dict(attempt_id=str(self.attempt), content=GOOD))
            counted = CountedConnection(conn)
            await record_summary(counted, dict(id=self.job, message_id=self.message), self.attempt, 'real_time')
            self.assertEqual(1, len(counted.reads))

    async def test_denial_does_not_remove_or_duplicate_existing_accepted_evidence(self):
        await self.prepare('scheduled')
        market = await self.post('list_match_markets', fixture_id='leyu:1')
        self.assertEqual(200, market.status_code)
        quote = market.json()['markets'][0]
        accepted = await self.post('record_expert_signals', signals=[{
            'fixture_id': 'leyu:1', 'quote_id': quote['quote_id'], 'evidence_text': '全场大2.5'}])
        self.assertEqual(200, accepted.status_code)
        self.assertEqual(1, len(accepted.json()['accepted']))
        original = await self.pool.fetchval('SELECT jsonb_agg(to_jsonb(s)) FROM v2_expert_signals s')
        summary, call = await self.review([BAD] * 3)
        self.assertFalse(summary['completion_verified'])
        self.assertEqual(3, call.await_count)
        receipt = await self.receipt(BAD)
        self.assertFalse(receipt['completion_verified'])
        self.assertEqual(1, receipt['accepted_count'])
        self.assertEqual(original, await self.pool.fetchval('SELECT jsonb_agg(to_jsonb(s)) FROM v2_expert_signals s'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))
