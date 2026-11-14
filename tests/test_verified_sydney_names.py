import os
import unittest
from app.team_matching import team_name_matches,with_verified_aliases
import test_reviews as support


class SydneyIdentityTests(unittest.TestCase):
    def test_reviewed_club_alias_does_not_expand_to_other_squads_or_competitions(self):
        scope={'sport':'football','competition':'澳大利亚锦标赛'}
        self.assertTrue(team_name_matches('悉尼联','悉尼联58',**scope))
        for candidate in('悉尼联58U20','悉尼联58(女)','悉尼联58后备','悉尼FC'):
            self.assertFalse(team_name_matches('悉尼联',candidate,**scope))
        self.assertFalse(team_name_matches('悉尼联','悉尼联58',sport='football',competition='澳大利亚甲级联赛'))
        self.assertFalse(team_name_matches('悉尼联','悉尼联58',sport='basketball',competition='澳大利亚锦标赛'))
        fixture=dict(home_team='悉尼联58',away_team='南墨尔本',**scope)
        fixture['competition_name']=fixture.pop('competition')
        projected=with_verified_aliases(fixture)
        self.assertIn('悉尼联',projected['home_aliases'])
        self.assertIn('7m:7720',projected['verified_alias_sources']['home'])


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class SydneySearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_complete_source_pair_binds_reviewed_alias_once_without_candidate_refill(self):
        target=await self.fixture(home_team='悉尼联58',away_team='南墨尔本',competition_name='澳大利亚锦标赛')
        await self.fixture(2,home_team='悉尼联58U20',away_team='南墨尔本U20',competition_name='澳大利亚锦标赛')
        r=await self.post('search_matches',team_query='悉尼联',team_a='悉尼联',team_b='南墨尔本',
            competition_query='澳大利亚锦标赛',sport='football')
        self.assertEqual(200,r.status_code,r.text)
        value=r.json();self.assertTrue(value['search_complete'])
        self.assertEqual(target,value['resolution']['fixture']['id'])
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
