import os
import unittest
from app.competition_names import normalize_competition_name
from app.team_matching import team_name_matches,with_verified_aliases
import test_reviews as support

class PortugueseU23NamesTests(unittest.TestCase):
    def test_explicit_u23_identity_is_whole_and_competition_scoped(self):
        for a,b in [('法伦斯 U23','法鲁人U23'),('葡萄牙体育 U23','里斯本竞技U23')]:
            for league in ('葡U23','葡萄牙联赛U23','葡萄牙U23联赛'):
                self.assertTrue(team_name_matches(a,b,sport='football',competition=league))
            for name in (b.replace('U23',''),b.replace('U23','U19'),b+'B队',b+'(女)'):
                self.assertFalse(team_name_matches(a,name,sport='football',competition='葡U23'))
            self.assertFalse(team_name_matches(a,b,sport='basketball',competition='葡U23'))
            self.assertFalse(team_name_matches(a,b,sport='football',competition='葡萄牙超级联赛'))
        self.assertEqual('葡萄牙联赛u23',normalize_competition_name('葡U23','football'))
        self.assertEqual('葡u23',normalize_competition_name('葡U23',None))

@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class PortugueseU23SearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_full_source_pair_auto_binds_without_repeated_queries(self):
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2 WHERE id=$1',self.message,'葡U23 法伦斯 U23 0-0 葡萄牙体育 U23 半场0.75大')
        wanted=await self.fixture(home_team='法鲁人U23',away_team='里斯本竞技U23',competition_name='葡萄牙联赛U23')
        await self.fixture(2,home_team='法鲁人',away_team='里斯本竞技',competition_name='葡萄牙联赛U23')
        await self.fixture(3,home_team='法鲁人U19',away_team='里斯本竞技U19',competition_name='葡萄牙联赛U23')
        result=await self.post('search_matches',team_query='法伦斯 U23',team_a='法伦斯 U23',team_b='葡萄牙体育 U23',competition_query='葡U23',sport='football')
        self.assertEqual(200,result.status_code,result.text)
        value=result.json()
        self.assertTrue(value['search_complete'])
        self.assertTrue(value['resolution']['resolved'],value)
        self.assertEqual(wanted,value['resolution']['fixture']['id'])
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
