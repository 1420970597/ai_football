"""Source-order proof survives native resolution and evidence projection.

The API cases use task-owned PostgreSQL fixtures. They never call a model,
collector, account, or trading endpoint.
"""
import os
import unittest
import uuid

from app.review_tool_evidence import replay_snapshot, snapshot_result
from tests import test_reviews as fixtures


class SourceTeamSideProjectionTests(unittest.TestCase):
    def test_valid_bijections_are_preserved_in_resolve_and_search_snapshots(self):
        for sides in ({'team_a': 'home', 'team_b': 'away'},
                      {'team_a': 'away', 'team_b': 'home'}):
            with self.subTest(sides=sides):
                native = {'resolved': True, 'source_team_sides': dict(sides),
                          'fixture': {'id': 'fixture', 'internal_only': 'omit'}}
                resolved = snapshot_result('mcp_resolve_match', native)
                self.assertEqual(sides, resolved['source_team_sides'])
                self.assertEqual({'id': 'fixture'}, resolved['fixture'])
                searched = snapshot_result('mcp_search_matches', {
                    'items': [], 'search_key': 'a' * 64, 'search_complete': True,
                    'next_cursor': None, 'total_candidates': 1,
                    'resolution': native})
                self.assertEqual(resolved, searched['resolution'])
                self.assertEqual(sides, native['source_team_sides'])

    def test_missing_ambiguous_or_extra_sides_are_not_projected(self):
        for sides in (None, {}, {'team_a': 'home'},
                      {'team_a': 'home', 'team_b': 'home'},
                      {'team_a': 'away', 'team_b': 'away'},
                      {'team_a': 'home', 'team_b': 'away', 'guessed': True},
                      {'team_a': 'host', 'team_b': 'guest'},
                      {'team_a': 1, 'team_b': 2}):
            with self.subTest(sides=sides):
                self.assertNotIn('source_team_sides', snapshot_result(
                    'mcp_resolve_match', {'resolved': True, 'source_team_sides': sides}))

    def test_malformed_unhashable_sides_fail_closed_without_breaking_receipt(self):
        for sides in ({'team_a': [], 'team_b': 'away'},
                      {'team_a': 'home', 'team_b': {'side': 'away'}}):
            with self.subTest(sides=sides):
                self.assertEqual({'resolved': True}, snapshot_result(
                    'mcp_resolve_match', {'resolved': True, 'source_team_sides': sides}))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class SourceTeamSideAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ReviewTests.asyncSetUp
    asyncTearDown = fixtures.ReviewTests.asyncTearDown
    context = fixtures.ReviewTests.context
    post = fixtures.ReviewTests.post
    fixture = fixtures.ReviewTests.fixture
    search_all = fixtures.ReviewTests.search_all

    async def resolve_pair(self, key, team_a, team_b):
        response = await self.post('resolve_match', search_key=key,
                                   team_a=team_a, team_b=team_b)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    async def test_source_order_is_independent_from_provider_home_away(self):
        await self.fixture()
        key, _ = await self.search_all(team='Alpha1')
        forward = await self.resolve_pair(key, 'Alpha1', 'Beta1')
        reversed_source = await self.resolve_pair(key, 'Beta1', 'Alpha1')
        self.assertEqual({'team_a': 'home', 'team_b': 'away'}, forward['source_team_sides'])
        self.assertEqual({'team_a': 'away', 'team_b': 'home'}, reversed_source['source_team_sides'])
        self.assertEqual(forward['fixture']['id'], reversed_source['fixture']['id'])
        self.assertTrue(reversed_source['fixture']['realtime_eligible'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))

    async def test_provider_aliases_prove_the_source_sides_without_changing_names(self):
        await self.fixture(home_aliases=['来源主队'], away_aliases=['来源客队'])
        key, _ = await self.search_all(team='来源客队')
        result = await self.resolve_pair(key, '来源客队', '来源主队')
        self.assertEqual({'team_a': 'away', 'team_b': 'home'}, result['source_team_sides'])
        self.assertEqual(('Alpha1', 'Beta1'),
                         (result['fixture']['home_team'], result['fixture']['away_team']))

    async def test_unique_fixture_with_two_possible_orientations_does_not_guess_sides(self):
        aliases = ['来源甲队', '来源乙队']
        await self.fixture(home_aliases=aliases, away_aliases=aliases)
        key, _ = await self.search_all(team='来源甲队')
        result = await self.resolve_pair(key, '来源甲队', '来源乙队')
        self.assertTrue(result['resolved'])
        self.assertNotIn('source_team_sides', result)
        self.assertEqual('fixture-1', result['fixture']['id'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))

    async def test_ambiguous_fixture_pair_has_no_mapping_or_binding(self):
        await self.fixture(1)
        await self.fixture(2, home_team='Alpha1', away_team='Beta1')
        key, _ = await self.search_all(team='Alpha1')
        result = await self.resolve_pair(key, 'Beta1', 'Alpha1')
        self.assertFalse(result['resolved'])
        self.assertEqual('ambiguous_fixture', result['reason'])
        self.assertEqual(2, result['candidate_count'])
        self.assertNotIn('source_team_sides', result)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))

    async def test_inline_mapping_only_appears_after_all_pages_and_replays_exactly(self):
        await self.fixture(1)
        await self.fixture(2)
        parameters = dict(team_query='Alpha', team_a='Beta1', team_b='Alpha1',
                          sport='football', page_size=1)
        response = await self.post('search_matches', **parameters)
        self.assertEqual(200, response.status_code, response.text)
        first = response.json()
        self.assertFalse(first['search_complete'])
        self.assertEqual('search_not_complete', first['resolution']['reason'])
        self.assertNotIn('source_team_sides', first['resolution'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        response = await self.post('search_matches', **parameters, cursor=first['next_cursor'])
        self.assertEqual(200, response.status_code, response.text)
        final = response.json()
        self.assertTrue(final['search_complete'])
        self.assertIsNone(final['next_cursor'])
        self.assertEqual('fixture-1', final['resolution']['fixture']['id'])
        self.assertEqual({'team_a': 'away', 'team_b': 'home'},
                         final['resolution']['source_team_sides'])
        trace = await self.pool.fetchrow(
            "SELECT payload FROM v2_analysis_traces WHERE job_id=$1 "
            "AND trace_kind='mcp_search_matches' ORDER BY sequence_no DESC LIMIT 1", self.job)
        manifest = trace['payload']['response_snapshot']
        chunks = await self.pool.fetch(
            "SELECT payload FROM v2_analysis_traces WHERE job_id=$1 "
            "AND trace_kind='tool_response_chunk' AND payload->>'call_id'=$2 ORDER BY sequence_no",
            self.job, trace['payload']['call_id'])
        self.assertEqual(final, replay_snapshot(manifest, [row['payload'] for row in chunks]))
        self.assertEqual('fixture-1', await self.pool.fetchval(
            'SELECT fixture_id FROM v2_review_fixture_bindings WHERE job_id=$1', self.job))

    async def test_cross_attempt_and_unauthorized_resolution_cannot_create_side_proof(self):
        await self.fixture()
        key, _ = await self.search_all(team='Alpha1')
        body = self.context(search_key=key, team_a='Beta1', team_b='Alpha1')
        wrong_attempt = await self.client.post('/internal/mcp/resolve_match',
                                              json=body | {'attempt_id': str(uuid.uuid4())})
        self.assertEqual(409, wrong_attempt.status_code)
        denied = await self.client.post('/internal/mcp/resolve_match', json=body,
                                        headers={'Authorization': 'Bearer unrelated'})
        self.assertEqual(401, denied.status_code)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        allowed = await self.resolve_pair(key, 'Beta1', 'Alpha1')
        self.assertEqual({'team_a': 'away', 'team_b': 'home'}, allowed['source_team_sides'])

    async def test_finished_match_retains_identity_proof_but_stays_ineligible(self):
        await self.fixture(phase='finished')
        key, _ = await self.search_all(team='Alpha1')
        result = await self.resolve_pair(key, 'Beta1', 'Alpha1')
        self.assertTrue(result['resolved'])
        self.assertEqual({'team_a': 'away', 'team_b': 'home'}, result['source_team_sides'])
        self.assertFalse(result['fixture']['realtime_eligible'])
        read = await self.post('list_match_markets', fixture_id='fixture-1')
        self.assertEqual(409, read.status_code)

