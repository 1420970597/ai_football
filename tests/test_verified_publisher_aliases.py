import os
import unittest
from app.competition_names import normalize_competition_name
from app.team_matching import team_name_matches,with_verified_aliases
import test_reviews as support

PAIRS=(('印度甲','印度甲级联赛','班加鲁鲁联','孟加拉鲁联合会','皇家克什米尔'),
       ('拉脱超降','拉脱维亚超级联赛 - 附加赛','瓦尔麦斯','瓦尔米耶拉','图库姆斯'),
       ('港超联杯','香港联赛杯','南区足球会','冠忠南区','沙田'))

class PublisherAliasTests(unittest.TestCase):
    def test_same_publisher_id_is_whole_name_and_scope_bound(self):
        for source,competition,a,b,opponent in PAIRS:
            with self.subTest(source=source):
                self.assertEqual(normalize_competition_name(competition,'football'),normalize_competition_name(source,'football'))
                for league in (source,competition):
                    self.assertTrue(team_name_matches(a,b,sport='football',competition=league))
                for decoy in (b+'U23',b+'B队',b+'(女)'):
                    self.assertFalse(team_name_matches(a,decoy,sport='football',competition=competition))
                for sport,league in ((None,competition),('basketball',competition),('football',competition+'青年'),('football','其他联赛')):
                    self.assertFalse(team_name_matches(a,b,sport=sport,competition=league))
                self.assertEqual(source,normalize_competition_name(source,'basketball'))
                fixture=with_verified_aliases(dict(sport='football',competition_name=competition,home_team=b,away_team=opponent))
                self.assertIn(a,fixture['home_aliases'])
                self.assertIn('titan:',fixture['verified_alias_sources']['home'])

@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class PublisherAliasSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_source_names_bind_only_exact_scoped_fixture_without_signals(self):
        for index,(source,competition,a,b,opponent)in enumerate(PAIRS,1):
            with self.subTest(source=source):
                await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2 WHERE id=$1',self.message,f'{source} {a} 0-0 {opponent} 半场大0.75')
                target=await self.fixture(index*10,home_team=b,away_team=opponent,competition_name=competition)
                await self.fixture(index*10+1,home_team=b+'U23',away_team=opponent+'U23',competition_name=competition)
                await self.fixture(index*10+2,home_team=b,away_team=opponent,competition_name=competition+'青年')
                response=await self.post('search_matches',team_query=a,team_a=a,team_b=opponent,competition_query=source,sport='football')
                self.assertEqual(200,response.status_code,response.text)
                value=response.json()
                self.assertTrue(value['search_complete'])
                self.assertTrue(value['resolution']['resolved'],value)
                self.assertEqual(target,value['resolution']['fixture']['id'])
                self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
