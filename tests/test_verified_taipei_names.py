import os
import unittest
from app.team_matching import team_name_matches,with_verified_aliases
import test_reviews as support


class TaipeiNameTests(unittest.TestCase):
    def test_exact_adult_source_labels_retain_identity_and_scope(self):
        for source in ('中国台湾','中国台湾(中)','Chinese Taipei'):
            self.assertTrue(team_name_matches(source,'中国台北',sport='football',competition='国际友谊赛'))
            self.assertTrue(team_name_matches(source,'中国台北',sport='football',competition='国际友谊'))
            for decoy in ('中国','中国台北U19','中国台北(女)','中国台湾U17'):
                self.assertFalse(team_name_matches(source,decoy,sport='football',competition='国际友谊赛'))
            for sport,league in ((None,'国际友谊赛'),('basketball','国际友谊赛'),('football','国际友谊赛U19'),('football','VS-国际友谊赛')):
                self.assertFalse(team_name_matches(source,'中国台北',sport=sport,competition=league))
        projected=with_verified_aliases(dict(sport='football',competition_name='国际友谊赛',home_team='中国台北',away_team='吉尔吉斯斯坦'))
        self.assertIn('中国台湾(中)',projected['home_aliases'])


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class TaipeiSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_original_source_labels_bind_the_exact_adult_pair_first_search(self):
        target=await self.fixture(1,home_team='中国台北',away_team='吉尔吉斯斯坦',competition_name='国际友谊赛')
        await self.fixture(2,home_team='中国',away_team='马来西亚',competition_name='国际友谊赛')
        await self.fixture(3,home_team='中国U19',away_team='塔吉克斯坦U19',competition_name='国际友谊赛U19')
        for source in ('中国台湾','中国台湾(中)'):
            response=await self.post('search_matches',sport='football',team_query=source,team_a=source,team_b='吉尔吉斯斯坦',competition_query='国际友谊')
            self.assertEqual(200,response.status_code,response.text)
            result=response.json();self.assertTrue(result['search_complete'])
            self.assertTrue(result['resolution']['resolved'],result)
            self.assertEqual(target,result['resolution']['fixture']['id'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
