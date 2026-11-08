"""Final stage claims need attempt-owned evidence, not model confidence."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest
import uuid

from app import worker
from app.review_state import trace
from app.review_summary import record_summary
import test_reviews as support


class CurrentPhaseClaimTests(unittest.TestCase):
    def claims(self, text):
        from app.review_phase_claims import current_phase_claims
        return current_phase_claims(text)

    def test_original_two_fixture_failure_has_two_attributed_claims(self):
        text = ('1. 系统比赛：水晶宫 对 利物浦（leyu:5803628），赛前，realtime_eligible=true。\n'
                '2. 系统比赛：伊普斯维奇 对 伯恩茅斯（leyu:5803626），赛前，realtime_eligible=true。')
        self.assertEqual([dict(fixture_id=f'leyu:{i}', claimed_phase='scheduled')
                          for i in (5803626, 5803628)], self.claims(text))

    def test_explicit_current_english_and_chinese_markdown_are_understood(self):
        for phase, expected in (('滚球', 'live'), ('未开赛', 'scheduled'), ('已完场', 'finished'),
                                ('取消/延期', 'cancelled'), ('LIVE', 'live')):
            with self.subTest(phase=phase):
                self.assertEqual([dict(fixture_id='leyu:1', claimed_phase=expected)],
                                 self.claims(f'**leyu:1**，当前状态：{phase}。'))

    def test_ambiguous_multiple_fixtures_are_not_cross_assigned(self):
        self.assertEqual([], self.claims('leyu:1 与 leyu:2，当前阶段：滚球。'))

    def test_quotes_fences_and_historical_descriptions_are_not_current_claims(self):
        for text in ('> leyu:1，赛前，', '```\nleyu:1，赛前，\n```',
                     '原文：leyu:1，赛前，', 'leyu:1，推荐时阶段：赛前。'):
            with self.subTest(text=text):
                self.assertEqual([], self.claims(text))

    def test_market_and_settlement_words_do_not_describe_current_phase(self):
        for text in ('leyu:1 已结算，赛前推荐小3.5。', 'leyu:1，滚球大小球盘口，未接受推荐。',
                     'leyu:1，当前阶段：未知。', 'leyu:1，phase: livelihood。'):
            with self.subTest(text=text):
                self.assertEqual([], self.claims(text))

    def test_canonical_ids_are_not_extracted_from_longer_tokens(self):
        for fixture in ('other:leyu:1', 'leyu:1extra', 'xleyu:1'):
            self.assertEqual([], self.claims(f'{fixture}，当前阶段：赛前。'))

    def test_two_contradictory_claims_are_both_preserved_for_verification(self):
        self.assertEqual([dict(fixture_id='leyu:1', claimed_phase=phase) for phase in ('live', 'scheduled')],
                         self.claims('leyu:1，当前阶段：滚球，phase=scheduled。'))

    def test_native_stage_wording_from_actual_replay_is_verified(self):
        for label in ('当前系统阶段 live', '系统阶段为 live', '系统阶段 live'):
            with self.subTest(label=label):
                self.assertEqual([dict(fixture_id='leyu:1', claimed_phase='live')],
                                 self.claims(f'系统比赛 leyu:1，{label}，可实时核验。'))

    def test_source_description_does_not_hide_separate_explicit_current_label(self):
        self.assertEqual([dict(fixture_id='leyu:1', claimed_phase='live')],
                         self.claims('按截图识别 leyu:1，当前系统阶段 live。'))
        self.assertEqual([], self.claims('leyu:1，引用“当前阶段：赛前”。'))


class SplitLineClaimTests(unittest.TestCase):
    def conflicts(self, text):
        from app.review_line_claims import split_line_claim_conflicts
        return split_line_claim_conflicts(text)

    def test_original_inconsistent_split_handicap_is_rejected(self):
        text = ('水晶宫对利物浦，原图已选水晶宫 +1/1.5，全场让球，对应主队受让0.75。'
                '已唯一绑定fixture leyu:5803628；当前没有报价。')
        conflict = self.conflicts(text)[0]
        self.assertEqual(('leyu:5803628', '1.25', '0.75'),
                         (conflict['fixture_id'], conflict['normalized_magnitude'], conflict['claimed_magnitude']))

    def test_correct_quarter_lines_and_native_home_away_sign_are_not_rejected(self):
        for source, derived in (('+1/1.5', '受让1.25'), ('-0.5/1', '让0.75'),
                                ('+0/0.5', '受让0.25'), ('-0.5/-1', '主队受让0.75')):
            self.assertEqual([], self.conflicts(f'leyu:1，已选 {source}，全场让球，对应{derived}。'))

    def test_odds_quotes_and_non_handicap_numbers_do_not_supply_a_line(self):
        for text in ('leyu:1，倍率@+1/1.5，对应主队受让0.75。',
                     '> leyu:1，已选+1/1.5，对应主队受让0.75。',
                     '原文引用：leyu:1，已选+1/1.5，对应主队受让0.75。',
                     'leyu:1，小1/1.5，对应总数0.75。'):
            self.assertEqual([], self.conflicts(text))

    def test_ambiguous_pairs_and_explicit_correction_are_not_false_conflicts(self):
        for text in ('leyu:1 与 leyu:2，已选+1/1.5，对应受让0.75。',
                     'leyu:1，已选+1/1.5与+0.5/1，对应受让0.75。',
                     'leyu:1，已选+1/1.5，不是对应受让0.75，应为1.25。'):
            self.assertEqual([], self.conflicts(text))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires task-owned isolated database')
class ReviewPhaseCompletionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def prepare(self, phase='live'):
        await self.fixture(1, id='leyu:1', phase=phase)
        page = await self.post('search_matches', team_query='Alpha')
        self.assertEqual(200, page.status_code, page.text)
        bound = await self.post('resolve_match', search_key=page.json()['search_key'],
                                team_a='Alpha1', team_b='Beta1')
        self.assertTrue(bound.json()['resolved'])

    async def review(self, texts):
        model = AsyncMock(side_effect=[(text, {'content': text}) for text in texts])
        job = dict(id=self.job, attempt_id=self.attempt, message={})
        with patch.object(worker, 'review_message', model):
            result = await worker._review_until_verified(self.pool, SimpleNamespace(), job)
        return result, model

    async def test_live_fixture_cannot_finish_as_scheduled_after_three_wrong_answers(self):
        await self.prepare()
        text = '系统比赛：Alpha1 对 Beta1（leyu:1），赛前，realtime_eligible=true。'
        (_, summary), model = await self.review([text] * 3)
        self.assertFalse(summary['completion_verified'])
        self.assertEqual(3, model.await_count)
        conflict = summary['completion_issues']['phase_claim_conflicts'][0]
        self.assertEqual(('leyu:1', 'scheduled', 'live'),
                         (conflict['fixture_id'], conflict['claimed_phase'], conflict['actual_phase']))

    async def test_correction_uses_existing_binding_and_does_not_repeat_market_or_signal(self):
        await self.prepare()
        (_, summary), model = await self.review([
            '系统比赛 leyu:1，当前状态：赛前。', '系统比赛 leyu:1，当前状态：滚球。'])
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(2, model.await_count)
        self.assertIn('阶段', model.await_args_list[1].kwargs['continuation'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_correct_current_phase_has_no_extra_model_turn(self):
        await self.prepare()
        (_, summary), model = await self.review(['leyu:1，当前阶段：滚球，无可用报价。'])
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(1, model.await_count)

    async def test_source_quote_and_recommendation_time_phase_are_not_current_claims(self):
        await self.prepare()
        text = '原文引用：leyu:1，赛前，小3.5。\n推荐时阶段：leyu:1，赛前。'
        (_, summary), model = await self.review([text])
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(1, model.await_count)

    async def test_foreign_attempt_binding_is_not_evidence_for_current_phase(self):
        await self.prepare()
        await self.pool.execute('UPDATE v2_review_fixture_bindings SET attempt_id=$2 WHERE job_id=$1',
                                self.job, uuid.uuid4())
        text = '系统比赛 leyu:1，当前阶段：赛前。'
        (_, summary), _ = await self.review([text] * 3)
        self.assertFalse(summary['completion_verified'])
        self.assertEqual(1, summary['completion_issues']['unbound_mention_count'])
        self.assertFalse(summary['completion_issues'].get('phase_claim_conflicts'))

    async def test_phase_change_after_binding_uses_current_fixture_evidence(self):
        await self.prepare('scheduled')
        await self.pool.execute("UPDATE v2_fixtures SET phase='live' WHERE id='leyu:1'")
        text = '系统比赛 leyu:1，当前阶段：赛前。'
        (_, summary), _ = await self.review([text] * 3)
        self.assertFalse(summary['completion_verified'])
        self.assertEqual('live', summary['completion_issues']['phase_claim_conflicts'][0]['actual_phase'])

    async def test_final_transaction_does_not_trust_model_success_flag(self):
        await self.prepare()
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'hermes_result', dict(attempt_id=str(self.attempt),
                content='系统比赛 leyu:1，当前阶段：赛前。', completion_verified=True))
            await record_summary(conn, dict(id=self.job, message_id=self.message), self.attempt, 'real_time')
        receipt = await self.pool.fetchval(
            "SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertFalse(receipt['completion_verified'])
        self.assertIn('阶段', receipt['content'])

    async def test_final_transaction_accepts_corrected_latest_answer(self):
        await self.prepare()
        async with self.pool.acquire() as conn, conn.transaction():
            for kind, phase in (('hermes_partial_result', '赛前'), ('hermes_result', '滚球')):
                await trace(conn, self.job, kind, dict(attempt_id=str(self.attempt),
                    content=f'系统比赛 leyu:1，当前阶段：{phase}。'))
            await record_summary(conn, dict(id=self.job, message_id=self.message), self.attempt, 'real_time')
        receipt = await self.pool.fetchval(
            "SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertTrue(receipt['completion_verified'])

    async def test_native_current_phase_wording_is_checked_beside_source_description(self):
        await self.prepare()
        text = '按截图识别 leyu:1，当前系统阶段 scheduled。'
        (_, summary), _ = await self.review([text] * 3)
        self.assertFalse(summary['completion_verified'])
        self.assertEqual('live', summary['completion_issues']['phase_claim_conflicts'][0]['actual_phase'])

    async def test_split_line_explanation_conflict_cannot_finish_as_verified(self):
        await self.prepare()
        text = 'leyu:1，原图已选+1/1.5，全场让球，对应主队受让0.75。'
        (_, summary), model = await self.review([text] * 3)
        self.assertFalse(summary['completion_verified'])
        self.assertEqual(3, model.await_count)
        self.assertEqual(1, summary['completion_issues']['display_line_conflict_count'])

    async def test_split_line_correction_does_not_repeat_binding_or_signal(self):
        await self.prepare()
        texts = ['leyu:1，已选+1/1.5，全场让球，对应主队受让0.75。',
                 'leyu:1，已选+1/1.5，全场让球，对应主队受让1.25。']
        (_, summary), model = await self.review(texts)
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(2, model.await_count)
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_transaction_rechecks_split_line_despite_model_success(self):
        await self.prepare()
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'hermes_result', dict(attempt_id=str(self.attempt),
                content='leyu:1，已选+1/1.5，全场让球，对应主队受让0.75。', completion_verified=True))
            await record_summary(conn, dict(id=self.job, message_id=self.message), self.attempt, 'real_time')
        receipt = await self.pool.fetchval(
            "SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertFalse(receipt['completion_verified'])
        self.assertIn('盘口', receipt['content'])
