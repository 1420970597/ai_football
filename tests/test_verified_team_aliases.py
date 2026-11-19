import os
import unittest

from app import team_matching as names
from app.review_tool_evidence import snapshot_result
import test_reviews as support


class VerifiedTeamAliasTests(unittest.TestCase):
    def test_maldonado_alias_is_limited_to_the_reviewed_senior_league(self):
        scope = {'sport': 'football', 'competition': '乌拉圭甲级联赛'}
        self.assertTrue(names.team_name_matches('马尔多纳多', '马尔多纳多体育', **scope))
        self.assertIn('马尔多纳多体育', names.team_search_variants('马尔多纳多', **scope))
        self.assertFalse(names.team_name_matches('马尔多纳多', '马尔多纳多体育U19', **scope))
        self.assertFalse(names.team_name_matches('马尔多纳多', '马尔多纳多体育', sport='football', competition='乌拉圭杯'))

    def test_koge_name_equivalence_is_limited_to_the_reviewed_competition(self):
        self.assertTrue(names.team_name_matches('勒尔科治女足', '科治(女)', sport='football', competition='欧洲女子冠军联赛'))
        self.assertFalse(names.team_name_matches('勒尔科治女足', '科治(女)', sport='football', competition='丹麦女子联赛'))
        self.assertFalse(names.team_name_matches('勒尔科治女足', '科治', sport='football', competition='欧洲女子冠军联赛'))

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

    async def test_maldonado_short_name_recalls_and_resolves_reviewed_fixture(self):
        wanted = await self.fixture(1, home_team='波士顿河', away_team='马尔多纳多体育',
                                    competition_name='乌拉圭甲级联赛')
        await self.fixture(2, home_team='波士顿河', away_team='马尔多纳多体育', competition_name='乌拉圭杯')
        await self.pool.execute("UPDATE v2_expert_messages SET raw_text='波士顿河 vs 马尔多纳多 大2.5' WHERE id=$1", self.message)
        response = await self.post('search_matches', team_query='马尔多纳多', sport='football',
                                   team_a='波士顿河', team_b='马尔多纳多')
        self.assertEqual(200, response.status_code, response.text)
        resolution = response.json()['resolution']
        self.assertTrue(resolution['resolved'], resolution)
        self.assertEqual(wanted, resolution['fixture']['id'])
        self.assertEqual('polymarket:uru1-bri-mal-2026-11-19+leyu:5875268',
                         resolution['fixture']['verified_alias_sources']['away'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_reviewed_womens_names_bind_only_the_champions_league_fixture(self):
        wanted = await self.fixture(1, home_team='巴黎FC(女)', away_team='科治(女)',
                                    competition_name='欧洲女子冠军联赛')
        await self.fixture(2, home_team='巴黎FC(女)', away_team='科治(女)', competition_name='其他女子联赛')
        await self.pool.execute("UPDATE v2_expert_messages SET raw_text='巴黎FC女足 vs 勒尔科治女足 大2.5' WHERE id=$1",
                                self.message)
        response = await self.post('search_matches', team_query='勒尔科治女足', sport='football',
                                   team_a='巴黎FC女足', team_b='勒尔科治女足')
        self.assertEqual(200, response.status_code, response.text)
        resolution = response.json()['resolution']
        self.assertTrue(resolution['resolved'], resolution)
        self.assertEqual(wanted, resolution['fixture']['id'])
        self.assertEqual('uefa:2050572+leyu:5831449:20261119',
                         resolution['fixture']['verified_alias_sources']['away'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

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
