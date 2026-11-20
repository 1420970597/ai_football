"""Reviewed basketball translations bind original names, never a guessed opponent."""
from datetime import timedelta
import os
import unittest

from app import team_matching as names
import test_reviews as support


PAIRS = (
    ('高阳索诺天枪', '高阳天空枪手', '蔚山现代太阳神'),
    ('涩谷太阳摇滚', '东京太阳摇滚', '神户鹳鸟'),
)


class BasketballNameTests(unittest.TestCase):
    def test_reviewed_names_are_scoped_to_basketball_and_the_whole_team(self):
        for source, native, _ in PAIRS:
            with self.subTest(source=source):
                self.assertTrue(names.team_name_matches(source, native, sport='basketball'))
                for sport in (None, 'football'):
                    self.assertFalse(names.team_name_matches(source, native, sport=sport))
                for other in (native+'女篮', native+'U21', native+'预备队', '东京电击'):
                    self.assertFalse(names.team_name_matches(source, other, sport='basketball'))

    def test_official_english_identity_is_available_without_short_city_aliases(self):
        for source, native in (('Goyang Sono Skygunners', '高阳天空枪手'),
                               ('Sunrockers Shibuya', '东京太阳摇滚'),
                               ('Tokyo Sunrockers', '东京太阳摇滚')):
            self.assertTrue(names.team_name_matches(source, native, sport='basketball'))
        for source in ('高阳', '东京', '涩谷'):
            self.assertFalse(names.team_name_matches(source, '东京太阳摇滚', sport='basketball'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class BasketballAliasAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def native_fixture(self, index, native, home, **overrides):
        return await self.fixture(index, **dict(home_team=home, away_team=native,
            sport='basketball', competition_name='篮球联赛', phase='scheduled',
            kickoff_at=self.now+timedelta(hours=1)) | overrides)

    async def test_original_opponent_names_retrieve_bind_and_preserve_native_rows(self):
        for index, (source, native, home) in enumerate(PAIRS, 1):
            wanted = await self.native_fixture(index, native, home)
            response = await self.post('search_matches', team_query=source, sport='basketball',
                                       team_a=home, team_b=source)
            self.assertEqual(200, response.status_code, response.text)
            result = response.json()
            self.assertTrue(result['search_complete'])
            self.assertEqual([wanted], [row['id'] for row in result['items']])
            self.assertTrue(result['resolution']['resolved'])
            projected = result['items'][0]
            self.assertEqual(native, projected['away_team'])
            self.assertIn(source, projected['away_aliases'])
            evidence = await self.pool.fetchval(
                'SELECT evidence FROM v2_review_fixture_bindings WHERE job_id=$1 AND fixture_id=$2',
                self.job, wanted)
            self.assertEqual(projected['verified_alias_sources'], evidence['verified_alias_sources'])
            self.assertEqual([], await self.pool.fetchval('SELECT away_aliases FROM v2_fixtures WHERE id=$1', wanted))
        for table in ('v2_expert_signals', 'v2_bet_orders'):
            self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM '+table))

    async def test_wrong_opponent_and_same_pair_twice_remain_unresolved(self):
        source, native, home = PAIRS[0]
        await self.native_fixture(1, native, home)
        wrong = await self.post('search_matches', team_query=source, sport='basketball',
                                team_a='其他篮球队', team_b=source)
        self.assertFalse(wrong.json()['resolution']['resolved'])
        await self.native_fixture(2, native, home)
        ambiguous = await self.post('search_matches', team_query=home, sport='basketball',
                                    team_a=home, team_b=source)
        self.assertEqual(200, ambiguous.status_code, ambiguous.text)
        self.assertEqual('ambiguous_fixture', ambiguous.json()['resolution']['reason'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))

    async def test_other_sport_and_outside_requested_date_remain_complete_zero(self):
        source, native, home = PAIRS[1]
        await self.native_fixture(1, native, home, sport='football')
        await self.native_fixture(2, native, home, kickoff_at=self.now+timedelta(days=30))
        response = await self.post('search_matches', team_query=source, sport='basketball',
            team_a=home, team_b=source, from_utc=(self.now-timedelta(hours=2)).isoformat(),
            to_utc=(self.now+timedelta(hours=2)).isoformat())
        self.assertTrue(response.json()['search_complete'])
        self.assertEqual([], response.json()['items'])
        self.assertFalse(response.json()['resolution']['resolved'])
