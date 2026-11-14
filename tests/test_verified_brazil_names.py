import os
import unittest
from app.team_matching import team_name_matches,verified_team_search_variants
import test_reviews as support


class BrazilNamesTests(unittest.TestCase):
    def test_reviewed_names_require_actual_sport_league_and_squad(self):
        for source,target in (('雷加塔斯巴西','巴西雷加塔斯AL'),('CRB AL','巴西雷加塔斯AL'),('戈伊亚斯','戈亚斯GO')):
            self.assertTrue(team_name_matches(source,target,sport='football',competition='巴西乙级联赛'))
            for name in (target+'B',target+'U20',target+'(女)','博塔弗戈SP'):
                self.assertFalse(team_name_matches(source,name,sport='football',competition='巴西乙级联赛'))
            for sport,league in ((None,'巴西乙级联赛'),('basketball','巴西乙级联赛'),('football','巴西乙级联赛U20'),('football','巴西甲级联赛')):
                self.assertFalse(team_name_matches(source,target,sport=sport,competition=league))

    def test_unscoped_retrieval_never_grants_identity(self):
        self.assertIn('巴西雷加塔斯al',verified_team_search_variants('雷加塔斯巴西',sport='football')['football'])
        self.assertFalse(team_name_matches('雷加塔斯巴西','巴西雷加塔斯AL',sport='football'))

    def test_short_league_label_does_not_include_youth_women_or_virtual(self):
        from app.competition_names import competition_query_matches
        self.assertTrue(competition_query_matches('巴西乙','巴西乙级联赛','football'))
        for target in ('巴西乙级联赛U20','巴西乙级联赛(女)','虚拟巴西乙级联赛','巴西甲级联赛'):
            self.assertFalse(competition_query_matches('巴西乙',target,'football'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class BrazilSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_original_names_bind_first_complete_search_and_never_create_signals(self):
        for n,home,away,a,b in ((1,'巴西雷加塔斯AL','塞阿拉CE','雷加塔斯巴西','塞阿拉'),(2,'累西腓体育PE','戈亚斯GO','累西腓体育','戈伊亚斯')):
            target=await self.fixture(n,home_team=home,away_team=away,competition_name='巴西乙级联赛')
            for league in ('巴西乙',None):
                r=await self.post('search_matches',sport='football',team_query=a,team_a=a,team_b=b,competition_query=league)
                self.assertEqual(200,r.status_code,r.text)
                v=r.json();self.assertTrue(v['search_complete']);self.assertTrue(v['resolution']['resolved'],v)
                self.assertEqual(target,v['resolution']['fixture']['id'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_two_same_pair_fixtures_remain_ambiguous(self):
        for i in (1,2):await self.fixture(i,home_team='巴西雷加塔斯AL',away_team='塞阿拉CE',competition_name='巴西乙级联赛')
        v=(await self.post('search_matches',sport='football',team_query='雷加塔斯巴西',team_a='雷加塔斯巴西',team_b='塞阿拉',competition_query='巴西乙')).json()
        self.assertFalse(v['resolution']['resolved'])
        self.assertEqual('ambiguous_fixture',v['resolution']['reason'])

    async def test_other_league_cannot_gain_alias_binding(self):
        await self.fixture(1,home_team='巴西雷加塔斯AL',away_team='塞阿拉CE',competition_name='巴西甲级联赛')
        v=(await self.post('search_matches',sport='football',team_query='雷加塔斯巴西',team_a='雷加塔斯巴西',team_b='塞阿拉')).json()
        self.assertFalse(v['resolution']['resolved'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
