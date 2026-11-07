import os
import unittest

import test_reviews as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class CompetitionAliasTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def search(self, **extra):
        response = await self.post('search_matches', team_query='Alpha', phase='all', **extra)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    async def test_verified_abbreviations_keep_league_filter_and_auto_bind(self):
        for index, (alias, full) in enumerate((('英超', '英格兰超级联赛'), ('英冠', '英格兰冠军联赛')), 1):
            with self.subTest(alias=alias):
                fixture = await self.fixture(index, competition_name=full)
                for offset, decoy in enumerate(('英格兰联赛杯', '英格兰女子超级联赛'), 10):
                    await self.fixture(index * 100 + offset, competition_name=decoy,
                                       home_team=f'Alpha{index}', away_team=f'Beta{index}')
                result = await self.search(sport='football', competition_query=alias,
                                           team_a=f'Alpha{index}', team_b=f'Beta{index}')
                self.assertEqual([fixture], [x['id'] for x in result['items']])
                self.assertTrue(result['search_complete'])
                self.assertTrue(result['resolution']['resolved'])
                scope = await self.pool.fetchval(
                    'SELECT competition_query FROM v2_match_searches WHERE job_id=$1 AND search_key=$2',
                    self.job, result['search_key'])
                self.assertEqual(alias, scope)

    async def test_explicit_resolution_uses_same_verified_alias(self):
        await self.fixture(competition_name='英格兰超级联赛')
        result = await self.search(sport='football', competition_query='英格兰超级联赛')
        response = await self.post('resolve_match', search_key=result['search_key'],
                                   team_a='Alpha1', team_b='Beta1', competition='英超')
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()['resolved'])

    async def test_different_league_alias_cannot_bind_same_team_pair(self):
        await self.fixture(competition_name='英格兰超级联赛')
        result = await self.search(sport='football', competition_query='英格兰超级联赛')
        response = await self.post('resolve_match', search_key=result['search_key'],
                                   team_a='Alpha1', team_b='Beta1', competition='英冠')
        self.assertEqual(200, response.status_code, response.text)
        self.assertFalse(response.json()['resolved'])
        self.assertEqual('no_matching_fixture', response.json()['reason'])

    async def test_pagination_restores_omitted_filter_but_rejects_changed_scope(self):
        await self.fixture(1, competition_name='英格兰超级联赛')
        await self.fixture(2, competition_name='英格兰超级联赛')
        first = await self.search(sport='football', competition_query='英超', page_size=1)
        self.assertIsNotNone(first['next_cursor'])
        changed = await self.post('search_matches', team_query='Alpha', phase='all', sport='football',
                                  competition_query='英格兰超级联赛', page_size=1, cursor=first['next_cursor'])
        self.assertEqual(409, changed.status_code)
        second = await self.post('search_matches', team_query='Alpha', phase='all',
                                 page_size=1, cursor=first['next_cursor'])
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual(first['search_key'], second.json()['search_key'])
        self.assertTrue(second.json()['search_complete'])
        self.assertNotEqual(first['items'][0]['id'], second.json()['items'][0]['id'])

    async def test_other_sport_unspecified_sport_and_unknown_suffix_do_not_expand(self):
        await self.fixture(competition_name='英格兰超级联赛')
        await self.fixture(2, sport='basketball', competition_name='英格兰超级联赛')
        for filters in ({'sport': 'basketball', 'competition_query': '英超'},
                        {'competition_query': '英超'},
                        {'sport': 'football', 'competition_query': '英超青年'}):
            with self.subTest(filters=filters):
                result = await self.search(**filters)
                self.assertEqual([], result['items'])

    async def test_full_name_and_empty_filter_keep_existing_behavior(self):
        await self.fixture(competition_name='英格兰超级联赛')
        await self.fixture(2, competition_name='英格兰联赛杯')
        self.assertEqual(1, len((await self.search(sport='football', competition_query='英格兰超级联赛'))['items']))
        self.assertEqual(2, len((await self.search(sport='football'))['items']))
