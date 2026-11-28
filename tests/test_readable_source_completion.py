"""Original image captions and failed vision must not silently become no picks."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

from app import worker
from app.review_completion import explicit_text_pick
from app.review_state import trace
from app.review_summary import record_summary
import test_reviews as support


class CurrentCaptionTests(unittest.TestCase):
    def test_original_current_following_suffix_keeps_period_pick_explicit(self):
        for text in ('半场 0.5/1 大 等水上车', '半場 0.5/1 大 等水上車'):
            self.assertTrue(explicit_text_pick(text))
        for text in ('昨天半场0.5/1大 等水上车', '半场0.5/1大 等水上车？',
                     '取消半场0.5/1大 等水上车', '> 半场0.5/1大 等水上车',
                     '```\n半场0.5/1大 等水上车\n```'):
            self.assertFalse(explicit_text_pick(text))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class ReadableSourceCompletionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post

    def source_message(self, text='', *, image=True):
        return dict(raw_text=text, media_objects=[dict(mime_type='image/jpeg')]
                    if image else [])

    async def review(self, text, answer, *, image=True):
        call = AsyncMock(return_value=(answer, {}))
        with patch.object(worker, 'review_message', call):
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(),
                dict(id=self.job, attempt_id=self.attempt, message=self.source_message(text, image=image)))
        return call, summary

    async def receipt(self, text, answer):
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2,media_objects=$3 WHERE id=$1',
            self.message, text, [dict(mime_type='image/jpeg', object_key='isolated-image')])
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'hermes_result', dict(attempt_id=str(self.attempt),
                content=answer, completion_verified=True))
            await record_summary(conn, dict(id=self.job, message_id=self.message), self.attempt, 'real_time')
        return await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")

    async def test_original_following_caption_cannot_complete_without_identity_review(self):
        call, summary = await self.review('半场 0.5/1 大 等水上车',
            '这个消息没有明确的球队，也没有新增推荐可选，我不搜索也不录入。')
        self.assertEqual(3, call.await_count)
        self.assertFalse(summary['completion_verified'])
        self.assertTrue(summary['completion_issues']['text_intent_unverified'])

    async def test_bare_image_total_and_alternative_request_review_without_choosing_a_line(self):
        call, summary = await self.review('大2.5/3 2.5', '没有新增推荐，无法确认比赛。')
        self.assertEqual(3, call.await_count)
        self.assertFalse(summary['completion_verified'])
        self.assertTrue(summary['completion_issues']['text_intent_unverified'])
        for table in ('v2_review_fixture_bindings', 'v2_expert_signals', 'v2_bet_orders'):
            self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM '+table))

    async def test_unreadable_actual_image_stays_incomplete_after_bounded_review(self):
        call, summary = await self.review('', '图片中的文字我无法看清楚，因此无法确认比赛双方。')
        self.assertEqual(3, call.await_count)
        self.assertFalse(summary['completion_verified'])
        self.assertTrue(summary['completion_issues']['visual_access_unverified'])

    async def test_actual_image_correction_can_finish_as_non_recommendation(self):
        call = AsyncMock(side_effect=[('图片看不清，无法读取。', {}),
                                     ('已核对原图，这是表情包，没有球队或推荐。', {})])
        with patch.object(worker, 'review_message', call):
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(),
                dict(id=self.job, attempt_id=self.attempt, message=self.source_message()))
        self.assertEqual(2, call.await_count)
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_final_receipt_rechecks_actual_image_access_failure(self):
        receipt = await self.receipt('', '图片中的文字我无法看清楚，不能确认比赛。')
        self.assertFalse(receipt['completion_verified'])
        self.assertTrue(receipt['completion_issues']['visual_access_unverified'])

    async def test_final_receipt_rechecks_bare_total_from_actual_source_image(self):
        receipt = await self.receipt('大2.5/3 2.5', '没有新增推荐。')
        self.assertFalse(receipt['completion_verified'])
        self.assertTrue(receipt['completion_issues']['text_intent_unverified'])

    async def test_no_image_history_quote_hypothesis_and_correction_do_not_force_vision(self):
        cases = [('大2.5/3 2.5', '无新增推荐', False),
                 ('', '图片看不清', False),
                 ('昨天大2.5/3 2.5', '本条为历史反馈', True),
                 ('大1.3', '没有新增推荐', True),
                 ('', '> 图片看不清', True),
                 ('', '如果图片看不清，需要保留未解决。原图实际是表情。', True),
                 ('', '不能断言图片看不清。原图已核验为赛果反馈。', True),
                 ('', '上轮说明：图片看不清。当前原图是表情包。', True)]
        for text, answer, has_image in cases:
            with self.subTest(text=text, answer=answer):
                call, summary = await self.review(text, answer, image=has_image)
                self.assertEqual(1, call.await_count)
                self.assertTrue(summary['completion_verified'])
