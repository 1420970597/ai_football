"""Real U21 translation gaps must not weaken opponent, squad or sport checks."""
from datetime import datetime, timedelta, timezone
import os
import unittest

from app import team_matching as names
from app.competition_names import normalize_competition_name
import test_reviews as support


class ThaiU21NameTests(unittest.TestCase):
    def test_reviewed_whole_names_keep_the_exact_squad_and_sport(self):
        pairs = [('博乐大学 U21', '卡森邦迪特大学U21'), ('普拉查普 U21', '巴蜀基利U21')]
        for source, native in pairs:
            with self.subTest(source=source):
                self.assertTrue(names.team_name_matches(source, native, sport='football'))
                for sport in (None, 'basketball'):
                    self.assertFalse(names.team_name_matches(source, native, sport=sport))
                for other in (native.replace('U21', 'U19'), native.replace('U21', ''), native+'女足'):
                    self.assertFalse(names.team_name_matches(source, other, sport='football'))
        self.assertFalse(names.team_name_matches('博乐大学U21', '曼谷大学U21', sport='football'))
        for source in ('Kasem Bundit University', 'Kasem Bundit University U19'):
            self.assertFalse(names.team_name_matches(source, '卡森邦迪特大学U21', sport='football'))

    def test_competition_abbreviation_stays_scoped_to_football_u21(self):
        canonical = normalize_competition_name('泰国联赛U21', 'football')
        for source in ('泰U21', '泰国U21', 'Thai U21', 'Thailand U21'):
            self.assertEqual(canonical, normalize_competition_name(source, 'football'))
        self.assertNotEqual(canonical, normalize_competition_name('泰U21', 'basketball'))
        self.assertNotEqual(canonical, normalize_competition_name('泰U19', 'football'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires task-owned isolated database')
class ThaiU21AliasAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def native_fixture(self, index, **overrides):
        return await self.fixture(index, **dict(
            home_team='卡森邦迪特大学U21', away_team='巴蜀基利U21',
            competition_name='泰国联赛U21', sport='football', phase='live',
            kickoff_at=self.now + timedelta(hours=1)) | overrides)

    async def test_original_names_and_league_recall_the_one_native_pair(self):
        wanted = await self.native_fixture(1)
        await self.native_fixture(2, home_team='卡森邦迪特大学', away_team='巴蜀基利')
        await self.native_fixture(3, home_team='卡森邦迪特大学U19', away_team='巴蜀基利U19')
        await self.native_fixture(4, sport='basketball')
        response = await self.post('search_matches', team_query='博乐大学 U21', sport='football',
                                   competition_query='泰U21', team_a='博乐大学 U21', team_b='普拉查普 U21')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertTrue(result['search_complete'])
        self.assertEqual([wanted], [row['id'] for row in result['items']])
        self.assertTrue(result['resolution']['resolved'])
        self.assertEqual(wanted, result['resolution']['fixture']['id'])
        self.assertEqual('卡森邦迪特大学U21', result['items'][0]['home_team'])
        self.assertIn('博乐大学U21', result['items'][0]['home_aliases'])
        evidence = await self.pool.fetchval('SELECT evidence FROM v2_review_fixture_bindings WHERE job_id=$1', self.job)
        self.assertEqual(result['items'][0]['verified_alias_sources'], evidence['verified_alias_sources'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_wrong_opponent_and_duplicate_matches_are_not_authorized(self):
        await self.native_fixture(1)
        wrong = await self.post('search_matches', team_query='普拉查普U21', sport='football',
                                team_a='博乐大学U21', team_b='其他球队U21')
        self.assertEqual(200, wrong.status_code, wrong.text)
        self.assertFalse(wrong.json()['resolution']['resolved'])
        await self.native_fixture(2)
        ambiguous = await self.post('search_matches', team_query='博乐大学U21', sport='football',
                                    team_a='博乐大学U21', team_b='普拉查普U21')
        self.assertEqual(200, ambiguous.status_code, ambiguous.text)
        self.assertFalse(ambiguous.json()['resolution']['resolved'])
        self.assertEqual('ambiguous_fixture', ambiguous.json()['resolution']['reason'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))

    async def test_outside_requested_date_remains_complete_zero(self):
        now = datetime.now(timezone.utc)
        await self.native_fixture(1, kickoff_at=now + timedelta(days=30))
        response = await self.post('search_matches', team_query='博乐大学U21', sport='football',
                                   team_a='博乐大学U21', team_b='普拉查普U21',
                                   from_utc=(now-timedelta(hours=2)).isoformat(),
                                   to_utc=(now+timedelta(hours=2)).isoformat())
        self.assertTrue(response.json()['search_complete'])
        self.assertEqual([], response.json()['items'])
        self.assertFalse(response.json()['resolution']['resolved'])
