import os
import unittest
from app.team_matching import team_name_matches,with_verified_aliases,verified_team_search_variants
import test_reviews as support


class DutchNameTests(unittest.TestCase):
    def test_complete_alias_requires_reviewed_sport_league_and_squad(self):
        for source in ('迪加史卓普','De Graafschap'):
            self.assertTrue(team_name_matches(source,'格拉夫夏普',sport='football',competition='荷兰乙级联赛'))
            for target in ('格拉夫夏普U21','格拉夫夏普(女)','格拉夫夏普 B'):
                self.assertFalse(team_name_matches(source,target,sport='football',competition='荷兰乙级联赛'))
            for sport,league in ((None,'荷兰乙级联赛'),('basketball','荷兰乙级联赛'),('football','荷兰甲级联赛'),('football','荷兰乙级联赛U21'),('football','VS-荷兰乙级联赛')):
                self.assertFalse(team_name_matches(source,'格拉夫夏普',sport=sport,competition=league))
        projected=with_verified_aliases(dict(sport='football',competition_name='荷兰乙级联赛',home_team='格拉夫夏普',away_team='奥斯'))
        self.assertIn('迪加史卓普',projected['home_aliases'])
        self.assertEqual('奥斯',projected['away_team'])

    def test_missing_league_expands_retrieval_without_granting_identity(self):
        self.assertIn('格拉夫夏普',verified_team_search_variants('迪加史卓普',sport='football')['football'])
        self.assertFalse(team_name_matches('迪加史卓普','格拉夫夏普',sport='football'))
        self.assertEqual({},verified_team_search_variants('迪加史卓普',sport='football',competition='荷兰甲级联赛'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class DutchSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_original_names_bind_first_search_without_candidate_substitution(self):
        target=await self.fixture(1,home_team='格拉夫夏普',away_team='奥斯',competition_name='荷兰乙级联赛')
        await self.fixture(2,home_team='格拉夫夏普U21',away_team='奥斯U21',competition_name='荷兰乙级联赛U21')
        for league in ('荷蘭乙級聯賽',None):
            response=await self.post('search_matches',sport='football',team_query='迪加史卓普',team_a='迪加史卓普',team_b='奥斯',competition_query=league)
            self.assertEqual(200,response.status_code,response.text)
            result=response.json()
            self.assertTrue(result['search_complete'])
            self.assertTrue(result['resolution']['resolved'],result)
            self.assertEqual(target,result['resolution']['fixture']['id'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_two_same_pair_fixtures_still_refuse_ambiguous_identity(self):
        for index in (1,2):
            await self.fixture(index,home_team='格拉夫夏普',away_team='奥斯',competition_name='荷兰乙级联赛')
        response=await self.post('search_matches',sport='football',team_query='迪加史卓普',team_a='迪加史卓普',team_b='奥斯',competition_query='荷兰乙级联赛')
        self.assertEqual(200,response.status_code,response.text)
        self.assertFalse(response.json()['resolution']['resolved'])
        self.assertEqual('ambiguous_fixture',response.json()['resolution']['reason'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))

    async def test_unscoped_retrieval_never_binds_an_unverified_league(self):
        await self.fixture(1,home_team='格拉夫夏普',away_team='奥斯',competition_name='荷兰甲级联赛')
        response=await self.post('search_matches',sport='football',team_query='迪加史卓普',team_a='迪加史卓普',team_b='奥斯')
        self.assertEqual(200,response.status_code,response.text)
        self.assertFalse(response.json()['resolution']['resolved'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
