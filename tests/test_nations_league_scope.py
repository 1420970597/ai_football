import os
import unittest
from app.competition_names import competition_query_members,competition_query_matches,normalize_competition_name
import test_reviews as support


class NationsLeagueScopeTests(unittest.TestCase):
    def test_umbrella_query_has_only_four_explicit_mens_divisions(self):
        self.assertEqual(4,len(competition_query_members('欧国联','football')))
        for division in 'ABCD':
            self.assertTrue(competition_query_matches('欧国联','欧洲国家联赛'+division,'football'))
        for decoy in ('欧洲国家联赛','欧洲国家联赛X','欧洲国家联赛B女子','欧洲国家联赛B U21','VS-欧洲国家联赛B'):
            self.assertFalse(competition_query_matches('欧国联',decoy,'football'))
        for sport in (None,'basketball'):
            self.assertEqual((),competition_query_members('欧国联',sport))
            self.assertFalse(competition_query_matches('欧国联','欧洲国家联赛B',sport))

    def test_explicit_divisions_are_never_canonicalized_together(self):
        for a in 'ABCD':
            for b in 'ABCD':
                self.assertEqual(a==b,competition_query_matches('欧洲国家联赛'+a,'欧洲国家联赛'+b,'football'))
        self.assertNotEqual(normalize_competition_name('欧国联','football'),normalize_competition_name('欧洲国家联赛B','football'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class NationsLeagueSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_original_umbrella_query_binds_unique_pair_without_unfiltered_retry(self):
        target=await self.fixture(1,home_team='奥地利',away_team='爱尔兰',competition_name='欧洲国家联赛B')
        for index,name in enumerate(('欧洲国家联赛B女子','欧洲国家联赛B青年','欧洲国家联赛X','VS-欧洲国家联赛B'),2):
            await self.fixture(index,home_team='奥地利',away_team='爱尔兰',competition_name=name)
        response=await self.post('search_matches',phase='all',sport='football',team_query='奥地利',team_a='奥地利',team_b='爱尔兰',competition_query='欧国联')
        self.assertEqual(200,response.status_code,response.text)
        result=response.json();self.assertEqual([target],[f['id']for f in result['items']])
        self.assertTrue(result['search_complete']);self.assertTrue(result['resolution']['resolved'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_same_pair_in_two_divisions_remains_ambiguous(self):
        for index,division in enumerate('AB',1):
            await self.fixture(index,home_team='奥地利',away_team='爱尔兰',competition_name='欧洲国家联赛'+division)
        response=await self.post('search_matches',phase='all',sport='football',team_query='奥地利',team_a='奥地利',team_b='爱尔兰',competition_query='欧国联')
        result=response.json();self.assertEqual(200,response.status_code,response.text)
        self.assertFalse(result['resolution']['resolved']);self.assertEqual('ambiguous_fixture',result['resolution']['reason'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
