"""An omitted sport filter searches all sports without sharing identity evidence."""
import os
import unittest

import test_reviews as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires task-owned isolated database')
class UnspecifiedSportAliasTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def test_omitted_filter_uses_candidate_sport_for_verified_recall_and_resolution(self):
        wanted = await self.fixture(1, home_team='Alpha1', away_team='恰斯拉夫', sport='football')
        await self.fixture(2, home_team='Alpha1', away_team='恰斯拉夫', sport='basketball')
        response = await self.post('search_matches', team_query='卡拉夫', team_a='Alpha1', team_b='卡拉夫')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual([wanted], [f['id'] for f in result['items']])
        self.assertTrue(result['resolution']['resolved'])
        self.assertEqual('football', result['resolution']['fixture']['sport'])
        self.assertEqual('7m:4745:20261108', result['items'][0]['verified_alias_sources']['away'])

    async def test_explicit_other_sport_never_inherits_football_name_evidence(self):
        await self.fixture(1, home_team='Alpha1', away_team='恰斯拉夫', sport='football')
        await self.fixture(2, home_team='Alpha1', away_team='恰斯拉夫', sport='basketball')
        response = await self.post('search_matches', team_query='卡拉夫', sport='basketball',
                                   team_a='Alpha1', team_b='卡拉夫')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([], response.json()['items'])
        self.assertFalse(response.json()['resolution']['resolved'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
