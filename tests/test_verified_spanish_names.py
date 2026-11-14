import os
import unittest
from datetime import datetime,timedelta,timezone
from app.competition_names import normalize_competition_name
from app.team_matching import team_name_matches
import test_reviews as support


class SpanishScopeTests(unittest.TestCase):
    def test_exact_competition_labels_do_not_retrieve_other_countries_or_squads(self):
        for source,target in [('西乙','西班牙乙级联赛'),('西女超','西班牙女子甲级联赛')]:
            expected=normalize_competition_name(target,'football')
            self.assertEqual(expected,normalize_competition_name(source,'football'))
            for wrong in (source+'U19','巴'+source,source+'B','VS-'+source):
                self.assertNotEqual(expected,normalize_competition_name(wrong,'football'))
            self.assertNotEqual(expected,normalize_competition_name(source,'basketball'))

    def test_whole_espanyol_womens_names_preserve_gender_and_league(self):
        for source in ('爱斯宾奴(女)','愛斯賓奴女足'):
            self.assertTrue(team_name_matches(source,'西班牙人(女)',sport='football',competition='西女超'))
            for wrong in ('西班牙人','西班牙人B队','西班牙人U19'):
                self.assertFalse(team_name_matches(source,wrong,sport='football',competition='西女超'))
            self.assertFalse(team_name_matches(source,'西班牙人(女)',sport='football',competition='西女超U19'))

    def test_verified_tenerife_history_does_not_merge_men_reserves_or_unverified_short_names(self):
        for source in ('科斯塔阿德赫特内里费(女)','格拉纳迪拉特内里费苏女足','Costa Adeje Tenerife'):
            self.assertTrue(team_name_matches(source,'特内里费体育(女)',sport='football',competition='西女超'))
            for wrong in ('特内里费体育','特内里费B队','特内里费体育(女)U19','特内里费岛皇家联盟'):
                self.assertFalse(team_name_matches(source,wrong,sport='football',competition='西女超'))
            self.assertFalse(team_name_matches(source,'特内里费体育(女)',sport='basketball',competition='西女超'))
        self.assertFalse(team_name_matches('特内里费苏女足','特内里费体育(女)',sport='football',competition='西女超'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class SpanishSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_spanish_second_division_never_returns_brazilian_second_division(self):
        target=await self.fixture(1,home_team='阿尔梅里亚',away_team='安道尔',competition_name='西班牙乙级联赛')
        await self.fixture(2,home_team='米内罗美洲MG',away_team='阿瓦伊SC',competition_name='巴西乙级联赛')
        now=datetime.now(timezone.utc)
        response=await self.post('search_matches',sport='football',team_query='',team_a='阿尔梅里亚',team_b='安道尔',competition_query='西乙',from_utc=now.isoformat(),to_utc=(now+timedelta(days=2)).isoformat())
        self.assertEqual(200,response.status_code,response.text)
        result=response.json()
        self.assertEqual([target],[item['id']for item in result['items']])
        self.assertTrue(result['resolution']['resolved'])

    async def test_original_espanyol_query_retrieves_exact_womens_identity(self):
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2 WHERE id=$1',self.message,'西女超 爱斯宾奴(女) vs 科斯塔阿德赫特内里费(女)，全场大2.5')
        target=await self.fixture(1,home_team='西班牙人(女)',away_team='特内里费体育(女)',competition_name='西班牙女子甲级联赛')
        await self.fixture(2,home_team='西班牙人',away_team='特内里费体育',competition_name='西班牙乙级联赛')
        result=(await self.post('search_matches',sport='football',team_query='爱斯宾奴(女)',team_a='爱斯宾奴(女)',team_b='科斯塔阿德赫特内里费(女)',competition_query='西女超')).json()
        self.assertTrue(result['resolution']['resolved'],result)
        self.assertEqual(target,result['resolution']['fixture']['id'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*)FROM v2_expert_signals'))

    async def test_same_verified_pair_with_two_fixtures_still_requires_disambiguation(self):
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2 WHERE id=$1',self.message,'西女超 爱斯宾奴(女) vs 科斯塔阿德赫特内里费(女)，全场大2.5')
        for index in (1,2):
            await self.fixture(index,home_team='西班牙人(女)',away_team='特内里费体育(女)',competition_name='西班牙女子甲级联赛')
        result=(await self.post('search_matches',sport='football',team_query='爱斯宾奴(女)',team_a='爱斯宾奴(女)',team_b='科斯塔阿德赫特内里费(女)',competition_query='西女超')).json()
        self.assertFalse(result['resolution']['resolved'])
        self.assertEqual('ambiguous_fixture',result['resolution']['reason'])
