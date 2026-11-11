import os
import unittest
import uuid
from unittest.mock import patch

from app.review_completion import completion_issues
import test_reviews as support
from tests import test_review_tool_evidence as evidence_support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class SearchResolutionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture
    read_market = support.ReviewTests.read_market
    resolve = support.ReviewTests.resolve
    replay = evidence_support.ToolEvidenceAPITests.replay

    async def search(self, **kwargs):
        return await self.post('search_matches', team_query='Alpha', team_a='Alpha1', team_b='Beta1', **kwargs)

    async def bindings(self):
        return await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings')

    async def test_complete_mixed_name_spacing_binds_without_second_resolution(self):
        fixture = await self.fixture(home_team='CD印加', away_team='梅塔潘')
        await self.fixture(2, home_team='CD印加(女)', away_team='梅塔潘(女)')
        response = await self.post('search_matches', team_query='梅塔潘',
                                   team_a='CD 印加', team_b='梅塔潘')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertTrue(result['search_complete'])
        self.assertEqual(fixture, result['resolution']['fixture']['id'])
        self.assertEqual(1, await self.bindings())
        self.assertEqual(result, (await self.replay('mcp_search_matches'))[1])
        self.assertEqual(0, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='mcp_resolve_match'"))

    async def test_spaced_age_group_query_recalls_compact_provider_name_without_widening_identity(self):
        # 生产样本：来源写“雪兰莪 U20”，平台存“雪兰莪U20”；修复前同一场完整检索为零候选。
        fixture = await self.fixture(home_team='雪兰莪U20', away_team='柔佛达鲁塔克欣C队U20')
        await self.fixture(2, home_team='雪兰莪', away_team='柔佛达鲁塔克欣')
        response = await self.post('search_matches', team_query='雪兰莪 U20')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertTrue(result['search_complete'])
        self.assertEqual([fixture], [item['id'] for item in result['items']])
        self.assertNotIn('retrieval_basis', result['items'][0])
        self.assertEqual(0, await self.bindings())
        resolved = await self.post('resolve_match', search_key=result['search_key'],
                                   team_a='雪兰莪 U20', team_b='柔佛达鲁塔克欣C队 U20')
        self.assertEqual(200, resolved.status_code, resolved.text)
        self.assertTrue(resolved.json()['resolved'])
        self.assertEqual(fixture, resolved.json()['fixture']['id'])
        self.assertEqual(1, await self.bindings())

    async def test_spaced_query_recall_never_binds_adult_side_or_wrong_opponent(self):
        await self.fixture(home_team='雪兰莪U20', away_team='柔佛达鲁塔克欣C队U20')
        response = await self.post('search_matches', team_query='雪兰莪 U20',
                                   team_a='雪兰莪 U21', team_b='柔佛达鲁塔克欣C队 U21')
        self.assertEqual(200, response.status_code, response.text)
        self.assertFalse(response.json()['resolution']['resolved'])
        self.assertEqual(0, await self.bindings())

    async def test_mixed_name_spacing_still_requires_last_page_and_unique_fixture(self):
        await self.fixture(home_team='CD印加', away_team='梅塔潘')
        await self.fixture(2, home_team='CD 印加', away_team='梅塔潘')
        first = (await self.post('search_matches', team_query='梅塔潘',
            team_a='CD 印加', team_b='梅塔潘', page_size=1)).json()
        self.assertFalse(first['search_complete'])
        self.assertEqual('search_not_complete', first['resolution']['reason'])
        self.assertEqual(0, await self.bindings())
        last = (await self.post('search_matches', team_query='梅塔潘',
            team_a='CD 印加', team_b='梅塔潘', page_size=1, cursor=first['next_cursor'])).json()
        self.assertTrue(last['search_complete'])
        self.assertEqual({'resolved': False, 'reason': 'ambiguous_fixture', 'candidate_count': 2},
                         {key:last['resolution'][key]for key in ('resolved','reason','candidate_count')})
        self.assertEqual(2,len(last['resolution']['matching_candidates']))
        self.assertTrue(last['resolution']['matching_candidates_complete'])
        self.assertEqual(0, await self.bindings())

    async def test_complete_unique_search_binds_reads_records_and_passes_completion(self):
        fixture = await self.fixture()
        response = await self.search()
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertTrue(result['search_complete'])
        self.assertTrue(result['resolution']['resolved'])
        payload, replayed = await self.replay('mcp_search_matches')
        self.assertEqual(result, replayed)
        self.assertIn(fixture, payload['response_snapshot']['references']['fixture_ids'])
        self.assertEqual(fixture, result['resolution']['fixture']['id'])
        self.assertEqual(1, await self.bindings())
        quote = await self.read_market(fixture)
        recorded = await self.post('record_expert_signals', signals=[{
            'fixture_id': fixture, 'quote_id': quote['quote_id'], 'evidence_text': 'Alpha全场大2.5'}])
        self.assertEqual(1, len(recorded.json()['accepted']), recorded.text)
        async with self.pool.acquire() as conn:
            self.assertEqual({}, await completion_issues(conn, self.job, self.attempt, {fixture}))

    async def test_missing_pair_preserves_explicit_resolution_flow(self):
        await self.fixture()
        response = await self.post('search_matches', team_query='Alpha')
        self.assertNotIn('resolution', response.json())
        self.assertEqual(0, await self.bindings())
        await self.resolve(response.json()['search_key'])
        self.assertEqual(1, await self.bindings())

    async def test_explicit_resolution_uses_five_remote_statements_and_replay_is_exact(self):
        fixture = await self.fixture()
        search = (await self.post('search_matches', team_query='Alpha')).json()
        counted = evidence_support._CountingPool(self.pool)
        with patch('app.routes.reviews.database.current_pool', return_value=counted):
            response = await self.post('resolve_match', search_key=search['search_key'],
                                       team_a='Alpha1', team_b='Beta1')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(5, counted.commands)
        result = response.json()
        self.assertEqual(fixture, result['fixture']['id'])
        self.assertEqual(result, (await self.replay('mcp_resolve_match'))[1])
        self.assertEqual(1, await self.bindings())

    async def test_complete_empty_snapshot_returns_no_match_without_binding(self):
        search = (await self.post('search_matches', team_query='Absent')).json()
        self.assertTrue(search['search_complete'])
        self.assertEqual([], search['items'])
        response = await self.post('resolve_match', search_key=search['search_key'],
                                   team_a='Absent', team_b='Other')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({'resolved': False, 'reason': 'no_matching_fixture', 'candidate_count': 0},
                         response.json())
        self.assertEqual(0, await self.bindings())

    async def test_completed_snapshot_from_previous_attempt_cannot_resolve(self):
        await self.fixture()
        search = (await self.post('search_matches', team_query='Alpha')).json()
        self.attempt = uuid.uuid4()
        await self.pool.execute('UPDATE v2_analysis_jobs SET attempt_id=$1 WHERE id=$2',
                                self.attempt, self.job)
        response = await self.post('resolve_match', search_key=search['search_key'],
                                   team_a='Alpha1', team_b='Beta1')
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual('all candidate pages must be read first', response.json()['detail'])
        self.assertEqual(0, await self.bindings())

    async def test_partial_same_and_blank_teams_never_create_search_or_binding(self):
        await self.fixture()
        for pair in ({'team_a': 'Alpha'}, {'team_b': 'Beta'}, {'team_a': 'Alpha', 'team_b': 'Alpha'},
                     {'team_a': '  ', 'team_b': 'Beta'}, {'team_a': 'Alpha', 'team_b': '  '}):
            response = await self.post('search_matches', team_query='Alpha', **pair)
            self.assertEqual(422, response.status_code)
        self.assertEqual(0, await self.bindings())
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_match_searches'))

    async def test_no_binding_before_last_page_and_resolution_uses_all_pages(self):
        target = await self.fixture(1)
        await self.fixture(2, away_team='Gamma')
        first = (await self.search(page_size=1)).json()
        self.assertFalse(first['search_complete'])
        self.assertEqual('search_not_complete', first['resolution']['reason'])
        self.assertEqual(0, await self.bindings())
        final = (await self.search(page_size=1, cursor=first['next_cursor'])).json()
        self.assertTrue(final['search_complete'])
        self.assertEqual(target, final['resolution']['fixture']['id'])
        self.assertEqual(1, await self.bindings())

    async def test_bad_cursor_and_changed_attempt_never_bind(self):
        await self.fixture(1)
        await self.fixture(2, away_team='Gamma')
        first = (await self.search(page_size=1)).json()
        bad = await self.search(page_size=1, cursor=first['next_cursor']+'x')
        self.assertIn(bad.status_code, (400, 409))
        old = self.attempt
        self.attempt = uuid.uuid4()
        stale = await self.search(page_size=1, cursor=first['next_cursor'])
        self.attempt = old
        self.assertEqual(409, stale.status_code)
        self.assertEqual(0, await self.bindings())

    async def test_ambiguous_and_wrong_opponent_never_bind(self):
        await self.fixture(1)
        await self.fixture(2, home_team='Alpha1', away_team='Beta1')
        ambiguous = (await self.search()).json()
        self.assertFalse(ambiguous['resolution']['resolved'])
        self.assertEqual('ambiguous_fixture', ambiguous['resolution']['reason'])
        wrong = (await self.post('search_matches', team_query='Beta', team_a='Beta', team_b='Gamma')).json()
        self.assertEqual('no_matching_fixture', wrong['resolution']['reason'])
        self.assertEqual(0, await self.bindings())

    async def test_league_scope_disambiguates_and_terminal_fixture_is_not_tradeable(self):
        target = await self.fixture(1, competition_name='First League', phase='finished')
        await self.fixture(2, competition_name='Other League')
        response = (await self.search(competition_query='First League')).json()
        self.assertEqual(target, response['resolution']['fixture']['id'])
        self.assertFalse(response['resolution']['fixture']['realtime_eligible'])
        market = await self.post('list_match_markets', fixture_id=target)
        self.assertEqual(409, market.status_code)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_permission_lost_between_pages_prevents_inline_binding(self):
        await self.fixture(1)
        await self.fixture(2, away_team='Gamma')
        first = (await self.search(page_size=1)).json()
        await self.pool.execute('UPDATE v2_expert_sources SET enabled=false')
        response = await self.search(page_size=1, cursor=first['next_cursor'])
        self.assertEqual(409, response.status_code)
        self.assertEqual(0, await self.bindings())
