import os
import unittest
import uuid

from app.review_completion import completion_issues
import test_reviews as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class SearchResolutionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture
    read_market = support.ReviewTests.read_market
    resolve = support.ReviewTests.resolve

    async def search(self, **kwargs):
        return await self.post('search_matches', team_query='Alpha', team_a='Alpha', team_b='Beta', **kwargs)

    async def bindings(self):
        return await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings')

    async def test_complete_unique_search_binds_reads_records_and_passes_completion(self):
        fixture = await self.fixture()
        response = await self.search()
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertTrue(result['search_complete'])
        self.assertTrue(result['resolution']['resolved'])
        self.assertEqual(fixture, result['resolution']['fixture']['fixture_id'])
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

    async def test_partial_same_and_blank_teams_never_create_search_or_binding(self):
        await self.fixture()
        for pair in ({'team_a': 'Alpha'}, {'team_b': 'Beta'}, {'team_a': 'Alpha', 'team_b': 'Alpha'},
                     {'team_a': '  ', 'team_b': 'Beta'}, {'team_a': 'Alpha', 'team_b': '  '}):
            response = await self.post('search_matches', team_query='Alpha', **pair)
            self.assertEqual(422, response.status_code)
        self.assertEqual(0, await self.bindings())
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_match_searches'))

    async def test_no_binding_before_last_page_and_resolution_uses_all_pages(self):
        target = await self.fixture('target')
        await self.fixture('other', away_team='Gamma')
        first = (await self.search(page_size=1)).json()
        self.assertFalse(first['search_complete'])
        self.assertEqual('search_not_complete', first['resolution']['reason'])
        self.assertEqual(0, await self.bindings())
        final = (await self.search(page_size=1, cursor=first['next_cursor'])).json()
        self.assertTrue(final['search_complete'])
        self.assertEqual(target, final['resolution']['fixture']['fixture_id'])
        self.assertEqual(1, await self.bindings())

    async def test_bad_cursor_and_changed_attempt_never_bind(self):
        await self.fixture('one')
        await self.fixture('two', away_team='Gamma')
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
        await self.fixture('one')
        await self.fixture('two')
        ambiguous = (await self.search()).json()
        self.assertFalse(ambiguous['resolution']['resolved'])
        self.assertEqual('ambiguous_fixture', ambiguous['resolution']['reason'])
        wrong = (await self.post('search_matches', team_query='Beta', team_a='Beta', team_b='Gamma')).json()
        self.assertEqual('no_matching_fixture', wrong['resolution']['reason'])
        self.assertEqual(0, await self.bindings())

    async def test_league_scope_disambiguates_and_terminal_fixture_is_not_tradeable(self):
        target = await self.fixture('target', competition_name='First League', phase='finished')
        await self.fixture('other', competition_name='Other League')
        response = (await self.search(competition_query='First League')).json()
        self.assertEqual(target, response['resolution']['fixture']['fixture_id'])
        self.assertFalse(response['resolution']['fixture']['realtime_eligible'])
        market = await self.post('list_match_markets', fixture_id=target)
        self.assertEqual(409, market.status_code)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_permission_lost_between_pages_prevents_inline_binding(self):
        await self.fixture('one')
        await self.fixture('two', away_team='Gamma')
        first = (await self.search(page_size=1)).json()
        await self.pool.execute('UPDATE v2_expert_sources SET enabled=false')
        response = await self.search(page_size=1, cursor=first['next_cursor'])
        self.assertEqual(409, response.status_code)
        self.assertEqual(0, await self.bindings())
