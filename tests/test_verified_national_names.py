import os
import unittest
from app.team_matching import team_name_matches,with_verified_aliases,verified_team_search_variants
import test_reviews as support


class NationalNameTests(unittest.TestCase):
    def test_whole_adult_labels_are_limited_to_reviewed_division(self):
        for source,target in [('波斯尼亚和黑塞哥维那','波黑'),('波斯尼亞和黑塞哥維那','波黑'),('威尔斯','威尔士'),('威爾斯','威尔士'),('Wales','威尔士')]:
            self.assertTrue(team_name_matches(source,target,sport='football',competition='欧洲国家联赛B'))
            for decoy in (target+'U19',target+'(女)',target+'U21',target+' B'):
                self.assertFalse(team_name_matches(source,decoy,sport='football',competition='欧洲国家联赛B'))
            for sport,competition in [(None,'欧洲国家联赛B'),('basketball','欧洲国家联赛B'),('football','欧洲国家联赛A'),('football','欧洲国家联赛B U19'),('football','欧洲国家联赛B女子'),('football','VS-欧洲国家联赛B')]:
                self.assertFalse(team_name_matches(source,target,sport=sport,competition=competition))
        projected=with_verified_aliases(dict(sport='football',competition_name='欧洲国家联赛B',home_team='瑞典',away_team='波黑'))
        self.assertIn('波斯尼亚和黑塞哥维那',projected['away_aliases'])
        self.assertEqual('瑞典',projected['home_team'])

    def test_umbrella_retrieves_verified_division_without_granting_identity(self):
        result=verified_team_search_variants('威尔斯',sport='football',competition='欧国联')
        self.assertIn('威尔士',result['football'])
        self.assertFalse(team_name_matches('威尔斯','威尔士',sport='football',competition='欧国联'))
        for sport,competition in [('football','欧洲国家联赛A'),('basketball','欧国联'),('football','欧国联U19')]:
            self.assertEqual({},verified_team_search_variants('威尔斯',sport=sport,competition=competition))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class NationalSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_original_whole_names_resolve_both_teams_on_first_search(self):
        for index,home,away,source_home,source_away in [(1,'瑞典','波黑','瑞典','波斯尼亚和黑塞哥维那'),(2,'威尔士','比利时','威尔斯','比利时')]:
            target=await self.fixture(index,home_team=home,away_team=away,competition_name='欧洲国家联赛B')
            await self.fixture(index+10,home_team=home+'U19',away_team=away+'U19',competition_name='欧洲国家联赛B U19')
            response=await self.post('search_matches',sport='football',team_query=source_home,team_a=source_home,team_b=source_away,competition_query='欧国联')
            self.assertEqual(200,response.status_code,response.text)
            result=response.json();self.assertTrue(result['search_complete']);self.assertTrue(result['resolution']['resolved'],result)
            self.assertEqual(target,result['resolution']['fixture']['id'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_same_pair_twice_remains_ambiguous(self):
        for index in (1,2):
            await self.fixture(index,home_team='瑞典',away_team='波黑',competition_name='欧洲国家联赛B')
        response=await self.post('search_matches',sport='football',team_query='瑞典',team_a='瑞典',team_b='波斯尼亚和黑塞哥维那',competition_query='欧洲国家联赛B')
        self.assertEqual(200,response.status_code,response.text)
        self.assertFalse(response.json()['resolution']['resolved'])
        self.assertEqual('ambiguous_fixture',response.json()['resolution']['reason'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
