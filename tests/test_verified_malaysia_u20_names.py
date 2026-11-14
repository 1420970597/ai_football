"""The reviewed C U20 label and competition must not match another youth team."""
from datetime import timedelta
import os
import unittest

from app import team_matching as names
from app.competition_names import normalize_competition_name
import test_reviews as support


class MalaysiaU20NameTests(unittest.TestCase):
    def test_whole_c_team_u20_identity_keeps_team_level_age_and_sport(self):
        native = '柔佛达鲁塔克欣C队U20'
        self.assertTrue(names.team_name_matches('达罗塔晋C队U20', native, sport='football'))
        for wrong in ('柔佛达鲁塔克欣B队U20', native.replace('U20', 'U21'),
                      native.replace('U20', ''), native+'女足'):
            self.assertFalse(names.team_name_matches('达罗塔晋C队U20', wrong, sport='football'))
        self.assertFalse(names.team_name_matches('达罗塔晋C队U20', native, sport='basketball'))

    def test_original_competition_abbreviation_preserves_u20_and_sport(self):
        canonical = normalize_competition_name('马来西亚总统杯U20', 'football')
        self.assertEqual(canonical, normalize_competition_name('马统杯', 'football'))
        self.assertNotEqual(canonical, normalize_competition_name('马统杯', 'basketball'))
        self.assertNotEqual(canonical, normalize_competition_name('马来西亚总统杯U21', 'football'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class MalaysiaU20AliasAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def test_original_pair_and_league_bind_only_c_u20_with_provenance(self):
        args = dict(home_team='雪兰莪U20', away_team='柔佛达鲁塔克欣C队U20',
                    competition_name='马来西亚总统杯U20', sport='football', phase='live')
        wanted = await self.fixture(1, **args)
        await self.fixture(2, **(args | dict(away_team='柔佛达鲁塔克欣B队U20')))
        await self.fixture(3, **(args | dict(away_team='柔佛达鲁塔克欣C队U21')))
        response = await self.post('search_matches', team_query='达罗塔晋C队U20', sport='football',
            competition_query='马统杯', team_a='雪兰莪U20', team_b='达罗塔晋C队U20')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertTrue(result['search_complete'])
        self.assertEqual([wanted], [x['id'] for x in result['items']])
        self.assertTrue(result['resolution']['resolved'])
        stored = await self.pool.fetchval('SELECT evidence FROM v2_review_fixture_bindings WHERE job_id=$1', self.job)
        self.assertEqual(result['items'][0]['verified_alias_sources'], stored['verified_alias_sources'])
        self.assertEqual([], await self.pool.fetchval('SELECT away_aliases FROM v2_fixtures WHERE id=$1', wanted))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_duplicate_pair_and_wrong_opponent_do_not_gain_binding(self):
        args = dict(home_team='雪兰莪U20', away_team='柔佛达鲁塔克欣C队U20', sport='football')
        await self.fixture(1, **args)
        wrong = await self.post('search_matches', team_query='达罗塔晋C队U20', sport='football',
            team_a='槟城U20', team_b='达罗塔晋C队U20')
        self.assertFalse(wrong.json()['resolution']['resolved'])
        await self.fixture(2, **args)
        multiple = await self.post('search_matches', team_query='雪兰莪U20', sport='football',
            team_a='雪兰莪U20', team_b='达罗塔晋C队U20')
        self.assertEqual('ambiguous_fixture', multiple.json()['resolution']['reason'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
