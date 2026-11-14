import os
import unittest
from app.team_matching import team_name_matches, verified_team_search_variants
from app.competition_names import competition_query_matches
import test_reviews as support


class MirassolNamesTests(unittest.TestCase):
    def test_whole_name_mapping_requires_football_and_exact_adult_league(self):
        for name in ('米拉索', '米拉索尔', 'Mirassol FC'):
            self.assertTrue(team_name_matches(name, '迈拉索尔SP', sport='football', competition='巴西甲级联赛'))
        for target in ('迈拉索尔SPU20', '迈拉索尔SP(女)', '迈拉索尔SPB', '米内罗竞技'):
            self.assertFalse(team_name_matches('米拉索', target, sport='football', competition='巴西甲级联赛'))
        for sport, league in ((None, '巴西甲级联赛'), ('basketball', '巴西甲级联赛'), ('football', '巴西乙级联赛'), ('football', '巴西圣保罗州联赛'), ('football', 'VS- 巴西甲级联赛 PANDA独家EAFC24')):
            self.assertFalse(team_name_matches('米拉索', '迈拉索尔SP', sport=sport, competition=league))
        self.assertFalse(team_name_matches('米拉索', '迈拉索尔SP', sport='football'))

    def test_only_verified_complete_competition_labels_expand(self):
        for name in ('巴西甲', '巴甲'):
            self.assertTrue(competition_query_matches(name, '巴西甲级联赛', 'football'))
            self.assertIn('迈拉索尔sp', verified_team_search_variants('米拉索', sport='football', competition=name)['football'])
            self.assertFalse(competition_query_matches(name, '巴西乙级联赛', 'football'))
            self.assertFalse(competition_query_matches(name, '巴西甲级联赛', 'basketball'))
        for name in ('巴西甲女足', '巴西甲U20'):
            self.assertFalse(competition_query_matches(name, '巴西甲级联赛', 'football'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class MirassolSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def test_source_pair_resolves_only_verified_native_fixture_without_signal(self):
        target = await self.fixture(1, home_team='迈拉索尔SP', away_team='米内罗竞技', competition_name='巴西甲级联赛')
        await self.fixture(2, home_team='迈拉索尔SP', away_team='米内罗竞技', competition_name='VS- 巴西甲级联赛 PANDA独家EAFC24')
        for league in ('巴西甲', None):
            r = await self.post('search_matches', sport='football', team_query='米拉索', team_a='米拉索', team_b='米内罗竞技', competition_query=league)
            self.assertEqual(200, r.status_code, r.text)
            v = r.json()
            self.assertTrue(v['search_complete'])
            self.assertTrue(v['resolution']['resolved'], v)
            self.assertEqual(target, v['resolution']['fixture']['id'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_two_verified_real_fixtures_remain_ambiguous(self):
        for i in (1, 2):
            await self.fixture(i, home_team='迈拉索尔SP', away_team='米内罗竞技', competition_name='巴西甲级联赛')
        v = (await self.post('search_matches', sport='football', team_query='米拉索', team_a='米拉索', team_b='米内罗竞技', competition_query='巴西甲')).json()
        self.assertFalse(v['resolution']['resolved'])
        self.assertEqual('ambiguous_fixture', v['resolution']['reason'])
