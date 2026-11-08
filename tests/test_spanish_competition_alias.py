"""Observed source 西甲 must retain the exact provider competition scope."""
from datetime import timedelta
import os
import unittest

import test_reviews as support
from app.competition_names import normalize_competition_name


class SpanishCompetitionNameTests(unittest.TestCase):
    def test_verified_football_name_only(self):
        self.assertEqual('西班牙甲级联赛', normalize_competition_name('西甲', 'football'))
        for sport in (None, 'basketball'):
            self.assertEqual('西甲', normalize_competition_name('西甲', sport))
        for name in ('西甲青年', '西甲女子', '西班牙乙级联赛'):
            self.assertEqual(name, normalize_competition_name(name, 'football'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class SpanishCompetitionSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def search(self, **updates):
        arguments = dict(team_query='马德里竞技', sport='football', competition_query='西甲',
                         phase='all', from_utc=self.now.isoformat(),
                         to_utc=(self.now + timedelta(days=2)).isoformat())
        arguments.update(updates)
        response = await self.post('search_matches', **arguments)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    async def seed_pair(self, index=1, **updates):
        fields = dict(home_team='马德里竞技', away_team='巴塞罗那', competition_name='西班牙甲级联赛')
        fields.update(updates)
        return await self.fixture(index, **fields)

    async def test_source_pair_auto_binding_excludes_neighboring_competitions(self):
        expected = await self.seed_pair()
        for index, league in enumerate(('西班牙乙级联赛', '西班牙甲级联赛青年',
                                         '西班牙女子甲级联赛', '西班牙甲级联赛杯'), 2):
            await self.seed_pair(index, competition_name=league)
        await self.seed_pair(6, sport='basketball')
        result = await self.search(team_a='马德里竞技', team_b='巴塞罗那')
        self.assertEqual([expected], [row['id'] for row in result['items']])
        self.assertTrue(result['search_complete'])
        self.assertEqual(expected, result['resolution']['fixture']['id'])
        self.assertEqual('西甲', await self.pool.fetchval(
            'SELECT competition_query FROM v2_match_searches WHERE job_id=$1 AND search_key=$2',
            self.job, result['search_key']))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_explicit_resolution_uses_identical_exact_alias(self):
        expected = await self.seed_pair()
        result = await self.search(competition_query='西班牙甲级联赛')
        response = await self.post('resolve_match', search_key=result['search_key'],
                                   team_a='马德里竞技', team_b='巴塞罗那', competition='西甲')
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()['resolved'])
        self.assertEqual(expected, response.json()['fixture']['id'])

    async def test_complete_league_pagination_preserves_scope_and_date(self):
        expected = [await self.seed_pair(index) for index in (1, 2)]
        await self.seed_pair(3, kickoff_at=(self.now + timedelta(days=3)).isoformat())
        await self.seed_pair(4, competition_name='西班牙乙级联赛')
        first = await self.search(team_query='', page_size=1)
        self.assertEqual(2, first['total_candidates'])
        self.assertFalse(first['search_complete'])
        second = await self.search(team_query='', page_size=1, cursor=first['next_cursor'])
        self.assertTrue(second['search_complete'])
        self.assertEqual(first['search_key'], second['search_key'])
        self.assertEqual(expected, [first['items'][0]['id'], second['items'][0]['id']])

    async def test_unverified_sport_and_suffix_do_not_expand(self):
        await self.seed_pair()
        for changes in ({'sport': None}, {'sport': 'basketball'},
                        {'competition_query': '西甲青年'}, {'competition_query': '西甲女子'}):
            with self.subTest(changes=changes):
                self.assertEqual([], (await self.search(**changes))['items'])
