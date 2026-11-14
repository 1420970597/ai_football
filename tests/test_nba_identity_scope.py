"""NBA nicknames require exact competition; esports/G league are other events."""
import os
import unittest
from datetime import timedelta
from app.team_matching import team_name_matches, with_verified_aliases
import test_reviews as support


class NBAIdentityTests(unittest.TestCase):
    def test_short_names_only_expand_inside_exact_nba(self):
        for short, native in [('国王','萨克拉门托国王'),('开拓者','波特兰开拓者'),
                              ('快船','洛杉矶快船'),('雷霆','俄克拉荷马城雷霆'),
                              ('骑士','克利夫兰骑士'),('独行侠','达拉斯独行侠'),
                              ('灰熊','孟菲斯灰熊'),('篮网','布鲁克林篮网'),
                              ('森林狼','明尼苏达森林狼'),('公牛','芝加哥公牛'),
                              ('热火','迈阿密热火'),('活塞','底特律活塞')]:
            self.assertTrue(team_name_matches(short,native,sport='basketball',competition='NBA'))
            for competition in (None,'NBA G联盟','NBA - 梦幻对垒','VS- NBA 2K23 PANDA独家'):
                self.assertFalse(team_name_matches(short,native,sport='basketball',competition=competition))
            self.assertFalse(team_name_matches(short,native,sport='football',competition='NBA'))
            for suffix in ('女篮','U21','后备队'):
                self.assertFalse(team_name_matches(short,native+suffix,sport='basketball',competition='NBA'))

    def test_projection_never_adds_nba_aliases_to_virtual_events(self):
        fixture={'home_team':'萨克拉门托国王','away_team':'波特兰开拓者','sport':'basketball'}
        for league in ('NBA G联盟','NBA - 梦幻对垒','VS- NBA 2K23 PANDA独家'):
            result=with_verified_aliases(dict(fixture,competition_name=league))
            self.assertNotIn('verified_alias_sources',result)


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated PG')
class NBAIdentityDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_nba_short_pair_returns_only_actual_league_and_source_sides(self):
        for i,league in enumerate(('NBA','NBA G联盟','NBA - 梦幻对垒','VS- NBA 2K23 PANDA独家'),1):
            await self.fixture(i,home_team='萨克拉门托国王',away_team='波特兰开拓者',
                competition_name=league,sport='basketball',kickoff_at=self.now+timedelta(hours=1))
        result=(await self.post('search_matches',team_query='国王',team_a='国王',team_b='开拓者',
            competition_query='NBA',sport='basketball')).json()
        self.assertTrue(result['search_complete'])
        self.assertEqual(1,len(result['items']))
        self.assertTrue(result['resolution']['resolved'])
        self.assertEqual({'team_a':'home','team_b':'away'},result['resolution']['source_team_sides'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_wrong_opponent_and_duplicate_actual_fixture_stay_unresolved(self):
        for i in (1,2):
            await self.fixture(i,home_team='洛杉矶快船',away_team='俄克拉荷马城雷霆',
                competition_name='NBA',sport='basketball')
        wrong=(await self.post('search_matches',team_query='快船',team_a='快船',team_b='开拓者',
            competition_query='NBA',sport='basketball')).json()
        self.assertFalse(wrong['resolution']['resolved'])
        duplicate=(await self.post('search_matches',team_query='雷霆',team_a='快船',team_b='雷霆',
            competition_query='NBA',sport='basketball')).json()
        self.assertEqual('ambiguous_fixture',duplicate['resolution']['reason'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))

    async def test_new_original_short_pairs_bind_without_replacing_source_names(self):
        for index,(home,away,team_a,team_b)in enumerate((
            ('克利夫兰骑士','达拉斯独行侠','骑士','独行侠'),
            ('孟菲斯灰熊','布鲁克林篮网','灰熊','篮网'),
            ('明尼苏达森林狼','芝加哥公牛','森林狼','公牛'),
            ('迈阿密热火','底特律活塞','热火','活塞')),1):
            await self.fixture(index,home_team=home,away_team=away,sport='basketball',competition_name='NBA')
            result=(await self.post('search_matches',team_query=team_a,team_a=team_a,team_b=team_b,
                                   sport='basketball',competition_query='NBA')).json()
            self.assertTrue(result['resolution']['resolved'])
            self.assertEqual({'team_a':'home','team_b':'away'},result['resolution']['source_team_sides'])
            self.assertIn(team_a,result['resolution']['fixture']['home_aliases'])
            self.assertIn(team_b,result['resolution']['fixture']['away_aliases'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
