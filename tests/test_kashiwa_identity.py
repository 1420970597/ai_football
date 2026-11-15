import os
import unittest
from app.team_matching import team_name_matches,with_verified_aliases
from app.competition_names import normalize_competition_name
import test_reviews as support

class KashiwaIdentityTests(unittest.TestCase):
    def test_whole_publisher_identity_is_league_and_sport_bound(self):
        for league in ('日联杯','日本职业联赛杯'):
            self.assertTrue(team_name_matches('柏雷素尔','柏太阳神',sport='football',competition=league))
        for other in ('柏太阳神U21','柏太阳神B队','柏太阳神(女)'):
            self.assertFalse(team_name_matches('柏雷素尔',other,sport='football',competition='日联杯'))
        for sport,league in (('basketball','日联杯'),('football','日本J2联赛'),('football','日联杯青年')):
            self.assertFalse(team_name_matches('柏雷素尔','柏太阳神',sport=sport,competition=league))
        self.assertEqual('日联杯',normalize_competition_name('日联杯','basketball'))

@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class KashiwaSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    post=support.ReviewTests.post
    context=support.ReviewTests.context
    fixture=support.ReviewTests.fixture

    async def test_exact_source_pair_retrieves_and_resolves_without_signal_authority(self):
        target=await self.fixture(home_team='柏太阳神',away_team='川崎前锋',competition_name='日本职业联赛杯')
        response=await self.post('search_matches',team_query='柏雷素尔',team_a='柏雷素尔',team_b='川崎前锋',
            sport='football',competition_query='日联杯')
        self.assertEqual(200,response.status_code,response.text)
        self.assertTrue(response.json()['resolution']['resolved'])
        self.assertEqual(target,response.json()['resolution']['fixture']['id'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
