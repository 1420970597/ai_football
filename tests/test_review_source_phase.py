"""Source-supported phase refinement must preserve identity and price gates."""
import os
import unittest

from app.routes.reviews import ResolveInput
from app.review_resolution import resolve_candidates
import test_reviews as native
import test_mcp_composed_query as transport


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class SourcePhaseDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native.ReviewTests.asyncSetUp
    asyncTearDown = native.ReviewTests.asyncTearDown
    context = native.ReviewTests.context
    post = native.ReviewTests.post
    fixture = native.ReviewTests.fixture
    search_all = native.ReviewTests.search_all

    async def pair(self):
        await self.fixture(id='case-retired')
        await self.pool.execute("UPDATE v2_fixtures SET stale_projection_at=now() WHERE id='case-retired'")
        await self.fixture(id='case-live', phase='live')

    async def resolve(self, key, **extra):
        return await self.post('resolve_match', search_key=key, team_a='Alpha1', team_b='Beta1', **extra)

    async def test_complete_snapshot_refines_only_with_explicit_source_phase(self):
        await self.pair()
        key, rows = await self.search_all(page_size=1)
        self.assertEqual(2, len(rows))
        unknown = (await self.resolve(key)).json()
        self.assertEqual('ambiguous_fixture', unknown['reason'])
        live = (await self.resolve(key, source_phase='live')).json()
        self.assertTrue(live['resolved'])
        self.assertEqual('case-live', live['fixture']['id'])
        self.assertTrue(live['fixture']['realtime_eligible'])
        evidence = await self.pool.fetchval("SELECT evidence FROM v2_review_fixture_bindings WHERE fixture_id='case-live'")
        self.assertEqual('live', evidence['source_phase'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_incomplete_pages_and_same_phase_duplicates_still_reject(self):
        await self.pair()
        first = (await self.post('search_matches', team_query='Alpha', phase='all', page_size=1)).json()
        premature = await self.resolve(first['search_key'], source_phase='live')
        self.assertEqual(409, premature.status_code)
        await self.fixture(id='case-live-duplicate', phase='live')
        key, _ = await self.search_all(team='Alpha1', page_size=1)
        ambiguous = (await self.resolve(key, source_phase='live')).json()
        self.assertEqual('ambiguous_fixture', ambiguous['reason'])
        self.assertEqual(2, ambiguous['candidate_count'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))

    async def test_phase_does_not_override_team_or_eligibility(self):
        await self.pair()
        key, _ = await self.search_all()
        wrong = await self.post('resolve_match', search_key=key, team_a='OtherSide', team_b='Beta1', source_phase='live')
        self.assertEqual('no_matching_fixture', wrong.json()['reason'])
        retired = (await self.resolve(key, source_phase='scheduled')).json()
        self.assertTrue(retired['resolved'])
        self.assertFalse(retired['fixture']['realtime_eligible'])
        self.assertEqual('source_stopped_reporting', retired['fixture']['eligibility_reason'])

    async def test_phase_change_after_search_cannot_create_binding(self):
        await self.pair()
        key, _ = await self.search_all()
        await self.pool.execute("UPDATE v2_fixtures SET phase='finished' WHERE id='case-live'")
        result = (await self.resolve(key, source_phase='live')).json()
        self.assertFalse(result['resolved'])
        self.assertEqual('source_phase_changed', result['reason'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))


class SourcePhaseInputTests(unittest.TestCase):
    def test_unknown_all_and_market_period_are_not_fixture_phase(self):
        for phase in ('all', '', 'first_half', 'full_time', 'eligible'):
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                ResolveInput(review_id='00000000-0000-0000-0000-000000000001',
                             attempt_id='00000000-0000-0000-0000-000000000002',
                             search_key='a'*64, team_a='Alpha1', team_b='Beta1', source_phase=phase)


class SourcePhaseTransportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = transport.ComposedMarketQueryTests.asyncSetUp
    asyncTearDown = transport.ComposedMarketQueryTests.asyncTearDown
    rpc = transport.ComposedMarketQueryTests.rpc
    install = transport.ComposedMarketQueryTests.install
    call = transport.ComposedMarketQueryTests.call

    async def test_source_phase_reaches_native_resolver_only(self):
        calls = await self.install()
        result = await self.call('resolve_match', source_phase='live')
        self.assertFalse(result.get('isError', False))
        self.assertEqual('live', calls[0][1]['source_phase'])
        self.assertNotIn('source_phase', calls[1][1])
        self.assertEqual('full_time', calls[1][1]['period'])

    async def test_invalid_phase_has_zero_native_calls(self):
        calls = await self.install()
        result = await self.call('resolve_match', source_phase='all')
        self.assertTrue(result.get('isError'))
        self.assertEqual([], calls)
