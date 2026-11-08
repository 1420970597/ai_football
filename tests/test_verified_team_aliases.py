import os
import unittest

from app import team_matching as names
from app.review_tool_evidence import snapshot_result
import test_reviews as support


class VerifiedTeamAliasTests(unittest.TestCase):
    def test_alias_evidence_is_football_only_and_keeps_squad_identity(self):
        self.assertTrue(names.team_name_matches('卡拉夫', '恰斯拉夫', sport='football'))
        self.assertIn('恰斯拉夫', names.team_search_variants('卡拉夫', sport='football'))
        for sport in (None, 'basketball'):
            self.assertFalse(names.team_name_matches('卡拉夫', '恰斯拉夫', sport=sport))
        for other in ('恰斯拉夫U19', '恰斯拉夫B队', '卡拉路夫', '普利布兰B队'):
            self.assertFalse(names.team_name_matches('卡拉夫', other, sport='football'))
        self.assertFalse(names.team_name_matches('卡尔夫', '恰斯拉夫', sport='football'))

    def test_projection_preserves_original_and_upstream_aliases(self):
        fixture = dict(id='isolated', sport='football', home_team='Alpha', away_team='恰斯拉夫',
                       home_aliases=[], away_aliases=['upstream-alias'])
        result = names.with_verified_aliases(fixture)
        self.assertEqual(['upstream-alias'], fixture['away_aliases'])
        self.assertNotIn('verified_alias_sources', fixture)
        self.assertIn('upstream-alias', result['away_aliases'])
        self.assertIn('卡拉夫', result['away_aliases'])
        self.assertEqual('7m:4745:20261108', result['verified_alias_sources']['away'])
        self.assertEqual('恰斯拉夫', result['away_team'])

    def test_wire_only_preserves_declared_alias_provenance(self):
        sources = dict(registry_version='2026-11-08.1', away='7m:4745:20261108')
        fixture = dict(id='isolated', away_aliases=['卡拉夫'],
                       verified_alias_sources=sources | {'secret': 'must-not-leak'})
        result = snapshot_result('mcp_search_matches', dict(items=[fixture],
            search_complete=True, search_key='isolated', next_cursor=None, total_candidates=1))
        self.assertEqual(sources, result['items'][0].get('verified_alias_sources'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires task-owned isolated database')
class VerifiedTeamAliasAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def test_verified_name_recalls_fixture_and_emits_evidence(self):
        wanted = await self.fixture(1, home_team='Alpha1', away_team='恰斯拉夫')
        await self.fixture(2, home_team='Alpha1', away_team='恰斯拉夫', sport='basketball')
        response = await self.post('search_matches', team_query='卡拉夫', sport='football',
                                   team_a='Alpha1', team_b='卡拉夫')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual([wanted], [f['id'] for f in result['items']])
        self.assertTrue(result['resolution']['resolved'])
        self.assertEqual(wanted, result['resolution']['fixture']['id'])
        self.assertIn('卡拉夫', result['items'][0]['away_aliases'])
        self.assertEqual('7m:4745:20261108', result['items'][0]['verified_alias_sources']['away'])
        witness = await self.pool.fetchval(
            'SELECT evidence FROM v2_review_fixture_bindings WHERE job_id=$1 AND fixture_id=$2',
            self.job, wanted)
        self.assertEqual(result['resolution']['fixture']['verified_alias_sources'],
                         witness.get('verified_alias_sources'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_alias_does_not_bypass_other_team_squad_or_ambiguity(self):
        wanted = await self.fixture(1, home_team='Alpha1', away_team='恰斯拉夫')
        await self.fixture(2, home_team='Alpha1', away_team='恰斯拉夫U19')
        response = await self.post('search_matches', team_query='卡拉夫', sport='football',
                                   team_a='Wrong Club', team_b='卡拉夫')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertFalse(result['resolution']['resolved'])
        resolved = await self.post('resolve_match', search_key=result['search_key'],
                                    team_a='Alpha1', team_b='卡拉夫')
        self.assertTrue(resolved.json()['resolved'])
        self.assertEqual(wanted, resolved.json()['fixture']['id'])
        await self.fixture(3, home_team='Alpha1', away_team='恰斯拉夫')
        ambiguous = await self.post('search_matches', team_query='恰斯拉夫', sport='football',
                                    team_a='Alpha1', team_b='卡拉夫')
        self.assertEqual(200, ambiguous.status_code, ambiguous.text)
        self.assertFalse(ambiguous.json()['resolution']['resolved'])
        self.assertEqual('ambiguous_fixture', ambiguous.json()['resolution']['reason'])
