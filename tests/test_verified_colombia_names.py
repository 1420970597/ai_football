import os
import unittest
from app.team_matching import team_name_matches,verified_team_search_variants
from app.competition_names import competition_query_matches,normalize_competition_name
import test_reviews as support


class ColombiaNamesTests(unittest.TestCase):
    def test_reviewed_whole_name_requires_actual_league_sport_and_squad(self):
        self.assertTrue(team_name_matches('云斯卡尔达斯','卡尔达斯十一人',sport='football',competition='哥伦比亚甲级联赛'))
        for target in ('卡尔达斯十一人U20','卡尔达斯十一人(女)','卡尔达斯十一人B','国民竞技'):
            self.assertFalse(team_name_matches('云斯卡尔达斯',target,sport='football',competition='哥伦比亚甲级联赛'))
        for sport,league in ((None,'哥伦比亚甲级联赛'),('basketball','哥伦比亚甲级联赛'),('football','哥伦比亚乙级联赛'),('football','VS- 南美自由杯 PANDA独家EAFC24')):
            self.assertFalse(team_name_matches('云斯卡尔达斯','卡尔达斯十一人',sport=sport,competition=league))

    def test_autumn_is_only_a_documented_parent_retrieval_scope(self):
        self.assertEqual('哥伦甲秋',normalize_competition_name('哥伦甲秋','football'))
        for league in ('哥伦比亚甲级联赛','哥伦比亚甲组联赛'):
            self.assertTrue(competition_query_matches('哥伦甲秋',league,'football'))
        for league in ('哥伦比亚乙级联赛','哥伦比亚甲级联赛U20','哥伦比亚甲级联赛(女)','VS- 南美自由杯 PANDA独家EAFC24'):
            self.assertFalse(competition_query_matches('哥伦甲秋',league,'football'))
        self.assertFalse(competition_query_matches('哥伦甲秋','哥伦比亚甲级联赛','basketball'))
        self.assertIn('卡尔达斯十一人',verified_team_search_variants('云斯卡尔达斯',sport='football',competition='哥伦甲秋')['football'])
        self.assertFalse(team_name_matches('云斯卡尔达斯','卡尔达斯十一人',sport='football'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class ColombiaSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_original_names_and_autumn_scope_bind_only_real_fixture_without_signals(self):
        target=await self.fixture(1,home_team='国民竞技',away_team='卡尔达斯十一人',competition_name='哥伦比亚甲级联赛')
        await self.fixture(2,home_team='国民竞技',away_team='卡尔达斯十一人',competition_name='VS- 南美自由杯 PANDA独家EAFC24')
        for league in ('哥伦甲秋',None):
            r=await self.post('search_matches',sport='football',team_query='国民竞技',team_a='国民竞技',team_b='云斯卡尔达斯',competition_query=league)
            self.assertEqual(200,r.status_code,r.text)
            v=r.json();self.assertTrue(v['search_complete']);self.assertTrue(v['resolution']['resolved'],v)
            self.assertEqual(target,v['resolution']['fixture']['id'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_repeated_real_pair_is_ambiguous_not_forced_by_league_scope(self):
        for i in (1,2):await self.fixture(i,home_team='国民竞技',away_team='卡尔达斯十一人',competition_name='哥伦比亚甲级联赛')
        v=(await self.post('search_matches',sport='football',team_query='国民竞技',team_a='国民竞技',team_b='云斯卡尔达斯',competition_query='哥伦甲秋')).json()
        self.assertFalse(v['resolution']['resolved'])
        self.assertEqual('ambiguous_fixture',v['resolution']['reason'])
