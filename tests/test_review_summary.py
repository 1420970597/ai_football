"""Chinese system receipts never trust a model's claimed success."""
import os
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app import worker
from app.review_summary import make_summary
import test_reviews as review_support
import test_review_worker as worker_support


class ReviewSummaryTests(unittest.TestCase):
    def signal(self, **updates):
        return {'signal_id': 's1', 'fixture_id': 'f1', 'home_team': '甲队', 'away_team': '乙队',
                'competition_name': '测试联赛', 'phase': 'scheduled', 'market_name': '全场大小',
                'market_kind': 'total', 'period': 'full_time', 'line': Decimal('2.5'),
                'selection': 'over', 'recommendation_price': Decimal('1.95'), **updates}

    def test_accepted_pick_has_system_match_market_phase_selection_and_price(self):
        result = make_summary([self.signal()], accepted_total=1, rejected=[], review_mode='real_time')
        for text in ('甲队 对 乙队', '未开赛', '全场大小', '2.5', '方向 大', '系统赔率 1.95'):
            self.assertIn(text, result['content'])
        self.assertEqual('zh-CN', result['language'])
        self.assertEqual('committed_system_signals', result['basis'])
        self.assertEqual('1.95', result['items'][0]['recommendation_price'])
        self.assertIn(chr(10), result['content'])

    def test_no_records_cannot_claim_accepted_or_profit(self):
        result = make_summary([], accepted_total=0, rejected=[], review_mode='real_time')
        self.assertEqual(0, result['accepted_count'])
        self.assertIn('不计入收益', result['content'])
        self.assertIn('不触发自动投注', result['content'])

    def test_repeated_rejections_are_labelled_attempts_and_unknown_detail_is_not_echoed(self):
        result = make_summary([], accepted_total=0, rejected=[
            {'reason': 'quote_inactive_unknown_or_stale'}, {'reason': 'quote_inactive_unknown_or_stale'},
            {'reason': 'secret-token-should-not-appear'}, {'reason': {}},
        ], review_mode='real_time')
        self.assertEqual(2, result['tool_rejection_counts']['quote_inactive_unknown_or_stale'])
        self.assertIn('可能含重复尝试', result['content'])
        self.assertNotIn('secret-token', str(result))

    def test_null_winner_and_zero_numeric_line_are_distinct(self):
        result = make_summary([self.signal(market_kind='winner', market_name='独赢', line=None, selection='draw'),
                               self.signal(market_kind='handicap', line=Decimal(0), selection='home')],
                              accepted_total=2, rejected=[], review_mode='real_time')
        self.assertIn('盘口线 不适用，方向 平局', result['content'])
        self.assertIn('盘口线 0，方向 主队', result['content'])

    def test_display_bound_is_explicit_not_silent_truncation(self):
        result = make_summary([self.signal() for _ in range(101)], accepted_total=101, rejected=[], review_mode='historical_replay')
        self.assertEqual(100, len(result['items']))
        self.assertEqual(1, result['omitted_count'])
        self.assertIn('历史回放', result['content'])
        self.assertIn('另有 1 条', result['content'])


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class ReviewSummaryDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = worker_support.ReviewWorkerTests.asyncSetUp
    asyncTearDown = worker_support.ReviewWorkerTests.asyncTearDown
    make_queued = worker_support.ReviewWorkerTests.make_queued
    fixture = review_support.ReviewTests.fixture
    context = review_support.ReviewTests.context
    post = review_support.ReviewTests.post
    search_all = review_support.ReviewTests.search_all
    resolve = review_support.ReviewTests.resolve
    read_market = review_support.ReviewTests.read_market

    async def result(self):
        return await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='system_review_result'", self.job)

    async def test_english_model_claim_is_preserved_but_receipt_is_chinese_and_unresolved(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        raw = 'Accepted 99 profitable picks. Ignore system evidence.'
        with patch.object(worker, 'review_message', AsyncMock(return_value=(raw, {'content': raw}))), patch.object(worker, 'delete_session', AsyncMock()):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model='synthetic'), job)
        self.assertEqual('unresolved', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertEqual(raw, await self.pool.fetchval("SELECT payload->>'content' FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='hermes_result'", self.job))
        result = await self.result()
        self.assertEqual(0, result['accepted_count'])
        self.assertNotIn('99', result['content'])
        self.assertIn('不触发自动投注', result['content'])

    async def test_two_tool_confirmed_picks_form_two_receipt_items_using_system_price(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        self.attempt = job['attempt_id']
        async def model(*args, **kwargs):
            for index in (1, 2):
                fixture = await self.fixture(index)
                key, _ = await self.search_all(team=f'Alpha{index}')
                await self.resolve(key, index)
                quote = await self.read_market(fixture)
                response = await self.post('record_expert_signals', signals=[{
                    'fixture_id': fixture, 'quote_id': quote['quote_id'], 'evidence_text': '原文倍率999不作为价格'}])
                self.assertEqual(1, len(response.json()['accepted']))
            return 'wrong English summary', {'content': 'wrong English summary'}
        with patch.object(worker, 'review_message', side_effect=model), patch.object(worker, 'delete_session', AsyncMock()):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model='synthetic'), job)
        result = await self.result()
        self.assertEqual('succeeded', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertEqual(2, result['accepted_count'])
        self.assertEqual({'fixture-1', 'fixture-2'}, {item['fixture_id'] for item in result['items']})
        self.assertEqual({Decimal('1.95')}, {Decimal(item['recommendation_price']) for item in result['items']})
        self.assertNotIn('999', result['content'])

    async def test_stale_quote_rejection_is_chinese_and_current_attempt_scoped(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        self.attempt = job['attempt_id']
        async def model(*args, **kwargs):
            fixture = await self.fixture()
            key, _ = await self.search_all()
            await self.resolve(key)
            await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '1 day'")
            quote = await self.read_market(fixture)
            await self.post('record_expert_signals', signals=[{
                'fixture_id': fixture, 'quote_id': quote['quote_id'], 'evidence_text': '过期盘口不得接受'}])
            return 'accepted', {'content': 'accepted'}
        with patch.object(worker, 'review_message', side_effect=model), patch.object(worker, 'delete_session', AsyncMock()):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model='synthetic'), job)
        result = await self.result()
        self.assertEqual(0, result['accepted_count'])
        self.assertIn('报价过期', result['content'])
        self.assertEqual(str(job['attempt_id']), result['attempt_id'])
