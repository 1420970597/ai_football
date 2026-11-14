import os
import unittest
from app.competition_names import normalize_competition_name
from app.team_matching import team_name_matches, with_verified_aliases
import test_reviews as support


class AustralianWomenIdentityTests(unittest.TestCase):
    def test_short_league_is_sport_scoped_and_exact(self):
        self.assertEqual('澳大利亚女子甲级联赛', normalize_competition_name('澳女联', 'football'))
        for sport in ('basketball', None):
            self.assertEqual('澳女联', normalize_competition_name('澳女联', sport))
        self.assertEqual('澳女联u20', normalize_competition_name('澳女联U20', 'football'))

    def test_reviewed_entire_female_label_never_strips_squads(self):
        scope = dict(sport='football', competition='澳大利亚女子甲级联赛')
        self.assertTrue(team_name_matches('中央海岸水手 (女)', '中央海岸水手学院(女)', **scope))
        self.assertTrue(team_name_matches('中央海岸水手 (女)', '中央海岸水手学院(女)',
            sport='football', competition='澳女联'))
        for name in ('中央海岸水手学院', '中央海岸水手学院(女)U20',
                     '中央海岸水手学院(女)后备', '中央海岸水手U20(女)'):
            self.assertFalse(team_name_matches('中央海岸水手(女)', name, **scope))
        self.assertFalse(team_name_matches('中央海岸水手(女)', '中央海岸水手学院(女)',
            sport='football', competition='澳大利亚女子甲级联赛青年'))
        result = with_verified_aliases(dict(home_team='中央海岸水手学院(女)',
            away_team='珀斯光荣(女)', competition_name=scope['competition'], sport='football'))
        self.assertIn('中央海岸水手(女)', result['home_aliases'])
        self.assertIn('7m:9047', result['verified_alias_sources']['home'])


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class AustralianWomenSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def test_source_pair_and_abbreviated_league_auto_bind_first_search(self):
        await self.pool.execute("UPDATE v2_expert_messages SET raw_text=$2 WHERE id=$1", self.message,
            '澳女联 14:00\n中央海岸水手 (女) 0-0 珀斯光荣 (女)\n半场0.75大')
        target = await self.fixture(home_team='中央海岸水手学院(女)', away_team='珀斯光荣(女)',
            competition_name='澳大利亚女子甲级联赛')
        await self.fixture(2, home_team='中央海岸水手学院(女)', away_team='珀斯光荣(女)',
            competition_name='澳大利亚女子甲级联赛青年')
        await self.fixture(3, home_team='中央海岸水手学院U20(女)', away_team='珀斯光荣U20(女)',
            competition_name='澳大利亚女子甲级联赛')
        response = await self.post('search_matches', team_query='中央海岸水手 (女)',
            team_a='中央海岸水手 (女)', team_b='珀斯光荣 (女)',
            competition_query='澳女联', sport='football')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertTrue(result['search_complete'])
        self.assertTrue(result['resolution']['resolved'], result)
        self.assertEqual(target, result['resolution']['fixture']['id'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
