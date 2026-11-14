import unittest
from app.review_team_identity import added_squad_markers
import os
import test_reviews as support


class SourceIdentityTests(unittest.TestCase):
    def test_missing_reserve_women_and_age_cannot_be_invented(self):
        for team,source in [('惠灵顿凤凰后备队','惠灵顿凤凰VS奥克兰城'),
                            ('Alpha Reserves','Alpha vs Beta'),('Alpha Women','Alpha vs Beta'),
                            ('Alpha U23','Alpha vs Beta'),('Alpha U21','Alpha U19 vs Beta')]:
            with self.subTest(team=team):self.assertTrue(added_squad_markers(team,'Beta',source))

    def test_other_team_marker_does_not_authorize_selected_team_marker(self):
        self.assertTrue(added_squad_markers('Alpha Reserves','Beta Reserves','Alpha VS Beta Reserves'))

    def test_actual_source_markers_and_ordinary_names_are_preserved(self):
        for team,source in [('惠灵顿凤凰后备队','惠灵顿凤凰后备队VS奥克兰城'),
                            ('Alpha U23','Alpha U23 vs Beta'),('Alpha','Alpha vs Beta'),
                            ('女队甲','女子联赛 女队甲对乙队')]:
            with self.subTest(team=team):self.assertFalse(added_squad_markers(team,'Beta',source))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated PG')
class SourceIdentityDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_candidate_reserve_name_cannot_replace_unqualified_original(self):
        await self.fixture(1,home_team='惠灵顿凤凰后备队',away_team='奥克兰城')
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2 WHERE id=$1',
            self.message,'惠灵顿凤凰VS奥克兰城 足球（滚球）大/小 半场大1.25')
        page=(await self.post('search_matches',team_query='惠灵顿凤凰')).json()
        result=(await self.post('resolve_match',search_key=page['search_key'],
            team_a='惠灵顿凤凰后备队',team_b='奥克兰城')).json()
        self.assertFalse(result['resolved'])
        self.assertEqual('source_squad_identity_unverified',result['reason'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
