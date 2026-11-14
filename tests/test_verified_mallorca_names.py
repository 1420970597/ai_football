import os
import unittest
from app.team_matching import team_name_matches,verified_team_search_variants
import test_reviews as support


class MallorcaNameTests(unittest.TestCase):
    def test_reviewed_whole_names_require_sport_league_and_squad(self):
        for name in ('马略卡','馬略卡','RCD Mallorca','Real Club Deportivo Mallorca'):
            self.assertTrue(team_name_matches(name,'马洛卡',sport='football',competition='西班牙乙级联赛'))
            for target in ('马洛卡B','马洛卡U19','马洛卡(女)','皇家马德里'):
                self.assertFalse(team_name_matches(name,target,sport='football',competition='西班牙乙级联赛'))
            for sport,league in ((None,'西班牙乙级联赛'),('basketball','西班牙乙级联赛'),('football','西班牙乙级联赛U19'),('football','西班牙甲级联赛')):
                self.assertFalse(team_name_matches(name,'马洛卡',sport=sport,competition=league))

    def test_unscoped_retrieval_does_not_grant_identity(self):
        self.assertIn('马洛卡',verified_team_search_variants('马略卡',sport='football')['football'])
        self.assertFalse(team_name_matches('马略卡','马洛卡',sport='football'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class MallorcaSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_source_names_bind_first_complete_search_with_or_without_league(self):
        target=await self.fixture(1,home_team='马洛卡',away_team='科尔多瓦',competition_name='西班牙乙级联赛')
        await self.fixture(2,home_team='马洛卡B',away_team='科尔多瓦B',competition_name='西班牙乙级联赛B')
        for league in ('西乙',None):
            response=await self.post('search_matches',sport='football',team_query='马略卡',team_a='马略卡',team_b='科尔多瓦',competition_query=league)
            self.assertEqual(200,response.status_code,response.text)
            result=response.json();self.assertTrue(result['search_complete'])
            self.assertTrue(result['resolution']['resolved'],result)
            self.assertEqual(target,result['resolution']['fixture']['id'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_same_pair_two_fixtures_stays_ambiguous(self):
        for i in (1,2):await self.fixture(i,home_team='马洛卡',away_team='科尔多瓦',competition_name='西班牙乙级联赛')
        result=(await self.post('search_matches',sport='football',team_query='马略卡',team_a='马略卡',team_b='科尔多瓦',competition_query='西乙')).json()
        self.assertFalse(result['resolution']['resolved'])
        self.assertEqual('ambiguous_fixture',result['resolution']['reason'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))

    async def test_unscoped_wrong_league_cannot_bind(self):
        await self.fixture(1,home_team='马洛卡',away_team='科尔多瓦',competition_name='西班牙甲级联赛')
        result=(await self.post('search_matches',sport='football',team_query='马略卡',team_a='马略卡',team_b='科尔多瓦')).json()
        self.assertFalse(result['resolution']['resolved'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
