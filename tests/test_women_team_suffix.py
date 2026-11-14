import os
import unittest

from app.team_matching import team_name_matches, team_search_variants
import test_reviews as support


class WomenTeamSuffixTests(unittest.TestCase):
    def test_complete_women_identity_has_equivalent_explicit_suffix(self):
        for first, second in [('巴塞罗那B队女足', '巴塞罗那B队(女)'),
                              ('埃斯特雷马杜拉女足', '埃斯特雷马杜拉(女)'),
                              ('甲队女子', '甲队女队'), ('甲队（女）', '甲队女足')]:
            with self.subTest(first=first, second=second):
                self.assertTrue(team_name_matches(first, second, sport='football'))
                self.assertTrue(team_name_matches(second, first, sport='football'))
        self.assertIn('巴塞罗那b队女', team_search_variants('巴塞罗那B队女足', sport='football'))

    def test_women_club_reserve_and_age_identity_cannot_be_removed(self):
        for other in ('巴塞罗那B队', '巴塞罗那(女)', '巴塞罗那U19(女)',
                      '巴塞罗那B队U19(女)', '巴塞罗那C队(女)', '巴塞罗纳B队(女)'):
            self.assertFalse(team_name_matches('巴塞罗那B队女足', other, sport='football'))
        for sport in (None, 'basketball'):
            self.assertFalse(team_name_matches('巴塞罗那B队女足', '巴塞罗那B队(女)', sport=sport))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PG')
class WomenTeamSuffixDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def test_both_source_names_resolve_women_reserve_fixture(self):
        wanted = await self.fixture(1, home_team='巴塞罗那B队(女)', away_team='埃斯特雷马杜拉(女)')
        await self.fixture(2, home_team='巴塞罗那(女)', away_team='埃斯特雷马杜拉(女)')
        await self.fixture(3, home_team='巴塞罗那B队', away_team='埃斯特雷马杜拉')
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2 WHERE id=$1',
                               self.message, '巴塞罗那B队女足 VS 埃斯特雷马杜拉女足\n半场大1.5')
        result = (await self.post('search_matches', team_query='巴塞罗那B队女足', sport='football',
                                 team_a='巴塞罗那B队女足', team_b='埃斯特雷马杜拉女足')).json()
        self.assertTrue(result['resolution']['resolved'], result)
        self.assertEqual(wanted, result['resolution']['fixture']['id'])
        self.assertEqual({'team_a': 'home', 'team_b': 'away'}, result['resolution']['source_team_sides'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
