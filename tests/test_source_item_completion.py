"""A current explicit third pick cannot disappear after other picks succeed."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest
import uuid

from app import worker
from app.review_state import trace
from app.review_summary import record_summary
import test_reviews as support

SOURCE = ('推荐：蔚山现代太阳神+1.5分  （重注推荐）\n'
          '推荐：神户鹳鸟+11.5分\n推荐：全场172.5大')


class CurrentTotalItemTests(unittest.TestCase):
    def test_only_explicit_current_whole_period_total_creates_an_obligation(self):
        from app.review_intent_claims import explicit_current_total_items
        items = explicit_current_total_items(SOURCE)
        self.assertEqual([dict(period='full_time', line='172.5', selection='over',
                               source_text='全场172.5大')], items)
        for source in ('昨日\n推荐：全场172.5大', '> 推荐：全场172.5大',
                       '```\n推荐：全场172.5大\n```', '推荐：全场172.5大？',
                       '取消：全场172.5大', '半场0.75大', '全场大2.5 2.75',
                       '推荐：全场1.3大'):
            with self.subTest(source=source):
                self.assertEqual([], explicit_current_total_items(source))

    def test_current_reset_split_line_and_distinct_periods_remain_explicit(self):
        from app.review_intent_claims import explicit_current_total_items
        items = explicit_current_total_items('昨日\n推荐：全场大1.5\n今天推荐：上半场小0.5/1\n全场172.5大')
        self.assertEqual([('first_half', '0.75', 'under'), ('full_time', '172.5', 'over')],
                         [(x['period'], x['line'], x['selection']) for x in items])

    def test_pathological_item_count_stays_unverified(self):
        from app.review_intent_claims import explicit_current_total_items
        items = explicit_current_total_items('\n'.join(f'推荐：全场大{i}.5' for i in range(40)))
        self.assertEqual(33, len(items))
        self.assertEqual('source_total_item_budget_exceeded', items[-1]['reason'])

    def test_partial_or_non_numeric_read_filter_is_not_coverage(self):
        from app.review_source_items import _covers
        item = dict(period='full_time', line='172.5', selection='over')
        for scope, coverage in (({}, {}), ({'market_kind': 'team_total'}, {'complete_for_filter': True}),
                                ({'line': 'invalid'}, {'complete_for_filter': True}),
                                ({'market_query': '不明玩法'}, {'complete_for_filter': True})):
            with self.subTest(scope=scope):
                self.assertFalse(_covers(dict(market_filter=scope, market_coverage=coverage), item))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class SourceItemCompletionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture
    search_all = support.ReviewTests.search_all
    resolve = support.ReviewTests.resolve

    async def prepare(self):
        self.fixture_id = await self.fixture(1, markets=[dict(market_id='h', market_name='全场让分',
            kind='handicap', period='full_time', line='1.5', selection='home', price='1.95',
            status='active', source_updated_at=self.now.isoformat(), received_at=self.now.isoformat())])
        key, _ = await self.search_all()
        await self.resolve(key)
        response = await self.post('list_match_markets', fixture_id=self.fixture_id, market_kind='handicap')
        self.assertEqual(200, response.status_code, response.text)
        quote = response.json()['markets'][0]
        response = await self.post('record_expert_signals', signals=[dict(fixture_id=self.fixture_id,
            quote_id=quote['quote_id'], evidence_text='Alpha1 +1.5')])
        self.assertEqual(1, len(response.json()['accepted']))

    async def review(self, answer='另两项已处理；全场172.5大没有核验。', *, mode='real_time', callback=None):
        async def respond(*args, **kwargs):
            if callback and kwargs.get('continuation_round'):
                await callback()
            return answer, {}
        call = AsyncMock(side_effect=respond)
        with patch.object(worker, 'review_message', call):
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(),
                dict(id=self.job, attempt_id=self.attempt, review_mode=mode,
                     message=dict(raw_text=SOURCE, media_objects=[dict(mime_type='image/jpeg')])))
        return call, summary

    async def read_total(self, *, period='full_time', line='172.5', selection='over'):
        result = await self.post('list_match_markets', fixture_id=self.fixture_id,
            market_kind='total', period=period, line=line, selection=selection)
        self.assertEqual(200, result.status_code, result.text)

    async def test_other_accepted_pick_cannot_hide_unreviewed_explicit_total(self):
        await self.prepare()
        call, summary = await self.review()
        self.assertEqual(3, call.await_count)
        self.assertFalse(summary['completion_verified'])
        gaps = summary['completion_issues']['source_total_review_gaps']
        self.assertEqual('172.5', gaps[0]['line'])
        self.assertEqual('over', gaps[0]['selection'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_exact_read_scope_can_finish_as_unresolved_without_forced_recording(self):
        await self.prepare()
        call, summary = await self.review('该项已核对，当前没有有效总分报价，保持未解决。',
                                         callback=self.read_total)
        self.assertEqual(2, call.await_count)
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_other_period_line_or_direction_cannot_clear_source_item(self):
        await self.prepare()
        await self.read_total(period='first_half')
        await self.read_total(line='166.5')
        await self.read_total(selection='under')
        _, summary = await self.review()
        self.assertFalse(summary['completion_verified'])

    async def test_previous_attempt_read_cannot_clear_current_source_item(self):
        await self.prepare()
        await self.read_total()
        await self.pool.execute("UPDATE v2_analysis_traces SET payload=jsonb_set(payload,'{attempt_id}',"
            "to_jsonb($2::text)) WHERE job_id=$1 AND trace_kind='mcp_list_match_markets'",
            self.job, str(uuid.uuid4()))
        _, summary = await self.review()
        self.assertFalse(summary['completion_verified'])

    async def test_failed_partial_or_unbound_read_cannot_clear_source_item(self):
        await self.prepare()
        await self.read_total()
        await self.pool.execute("UPDATE v2_analysis_traces SET payload=jsonb_set(payload,'{status}',"
            "'\"failed\"'::jsonb) WHERE job_id=$1 AND trace_kind='mcp_list_match_markets'", self.job)
        self.assertFalse((await self.review())[1]['completion_verified'])
        await self.pool.execute("UPDATE v2_analysis_traces SET payload=jsonb_set("
            "jsonb_set(payload,'{status}','\"succeeded\"'::jsonb),"
            "'{market_coverage,complete_for_filter}','false'::jsonb) "
            "WHERE job_id=$1 AND trace_kind='mcp_list_match_markets'", self.job)
        self.assertFalse((await self.review())[1]['completion_verified'])
        await self.pool.execute("UPDATE v2_analysis_traces SET payload=jsonb_set(payload,"
            "'{market_coverage,complete_for_filter}','true'::jsonb) "
            "WHERE job_id=$1 AND trace_kind='mcp_list_match_markets'", self.job)
        await self.pool.execute('DELETE FROM v2_review_fixture_bindings WHERE job_id=$1', self.job)
        self.assertFalse((await self.review())[1]['completion_verified'])

    async def test_final_receipt_independently_checks_original_item(self):
        await self.prepare()
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2 WHERE id=$1', self.message, SOURCE)
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'hermes_result', dict(attempt_id=str(self.attempt),
                content='已处理两项，第三项没有核验。', completion_verified=True))
            await record_summary(conn, dict(id=self.job, message_id=self.message), self.attempt, 'real_time')
        receipt = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertFalse(receipt['completion_verified'])
        self.assertEqual(1, receipt['accepted_count'])
        self.assertIn('source_total_review_gaps', receipt['completion_issues'])

    async def test_historical_replay_does_not_request_current_market_reads(self):
        await self.prepare()
        call, summary = await self.review('历史消息仅保留分析。', mode='historical_replay')
        self.assertEqual(1, call.await_count)
        self.assertTrue(summary['completion_verified'])

    async def test_source_league_zero_result_requests_team_search_without_hard_league_filter(self):
        await self.fixture(1, home_team='卡塔尔U23', away_team='新加坡U23', competition_name='国际友谊赛')
        response = await self.post('search_matches', team_query='卡塔尔U23',
            team_a='卡塔尔U23', team_b='新加坡U23', sport='football', competition_query='国际友谊赛U23')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual(0, result['total_candidates'])
        self.assertEqual('search_source_team_without_competition_filter', result['next_search_guidance']['action'])
        self.assertEqual('卡塔尔U23', result['next_search_guidance']['team_query'])
        self.assertTrue(result['next_search_guidance']['does_not_confirm_identity'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
