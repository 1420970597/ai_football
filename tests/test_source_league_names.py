"""Observed TG whole-name league translations, never broad division rewriting."""
from datetime import timedelta
import os
import unittest

import test_reviews as support
from app.competition_names import normalize_competition_name


SOURCE_NAMES = (
    ('罗马尼亚甲组联赛', '罗马尼亚甲级联赛'),
    ('丹麦甲组联赛', '丹麦甲级联赛'),
    ('葡萄牙甲组联赛', '葡萄牙甲级联赛'),
)


class SourceLeagueNameTests(unittest.TestCase):
    def test_observed_whole_names_share_one_football_identity(self):
        for source, canonical in SOURCE_NAMES:
            with self.subTest(source=source):
                self.assertEqual(normalize_competition_name(canonical, 'football'),
                                 normalize_competition_name(source, 'football'))
                for sport in (None, 'basketball'):
                    self.assertNotEqual(normalize_competition_name(canonical, sport),
                                        normalize_competition_name(source, sport))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class SourceLeagueSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def search(self, **extra):
        response = await self.post('search_matches', phase='all', **extra)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    async def test_source_filter_returns_and_binds_only_the_exact_league(self):
        for index, (source, canonical) in enumerate(SOURCE_NAMES, 1):
            with self.subTest(source=source):
                wanted = await self.fixture(index, competition_name=canonical)
                for offset, decoy in enumerate((canonical + '青年', canonical + '女子',
                                                canonical.replace('甲级', '乙级')), 10):
                    await self.fixture(index * 100 + offset, competition_name=decoy,
                                       home_team=f'Alpha{index}', away_team=f'Beta{index}')
                result = await self.search(team_query=f'Alpha{index}', sport='football',
                    competition_query=source, team_a=f'Alpha{index}', team_b=f'Beta{index}')
                self.assertEqual([wanted], [r['id'] for r in result['items']])
                self.assertTrue(result['search_complete'])
                self.assertTrue(result['resolution']['resolved'])

    async def test_league_only_fallback_keeps_bounded_paging_and_rejects_changed_scope(self):
        for index, (source, canonical) in enumerate(SOURCE_NAMES, 1):
            with self.subTest(source=source):
                identities = [await self.fixture(index * 10 + j, competition_name=canonical,
                    kickoff_at=(self.now + timedelta(hours=j)).isoformat()) for j in (1, 2)]
                args = dict(team_query='', competition_query=source, sport='football', page_size=1,
                            from_utc=self.now.isoformat(),
                            to_utc=(self.now + timedelta(hours=3)).isoformat())
                first = await self.search(**args)
                self.assertIsNotNone(first['next_cursor'])
                changed = await self.post('search_matches', phase='all',
                    **(args | {'competition_query': canonical, 'cursor': first['next_cursor']}))
                self.assertEqual(409, changed.status_code)
                incomplete = await self.post('search_matches', phase='all', team_query='',
                                            page_size=1, cursor=first['next_cursor'])
                self.assertEqual(422, incomplete.status_code)
                second = await self.search(**(args | {'cursor': first['next_cursor']}))
                self.assertEqual(first['search_key'], second['search_key'])
                self.assertTrue(second['search_complete'])
                self.assertIsNone(second['next_cursor'])
                self.assertEqual(identities, [first['items'][0]['id'], second['items'][0]['id']])
                saved = await self.pool.fetchval(
                    'SELECT competition_query FROM v2_match_searches WHERE job_id=$1 AND search_key=$2',
                    self.job, first['search_key'])
                self.assertEqual(source, saved)

    async def test_explicit_resolution_uses_same_source_name_without_cross_league_binding(self):
        for index, (source, canonical) in enumerate(SOURCE_NAMES, 1):
            with self.subTest(source=source):
                await self.fixture(index, competition_name=canonical)
                result = await self.search(team_query=f'Alpha{index}', sport='football',
                                           competition_query=canonical)
                bound = await self.post('resolve_match', search_key=result['search_key'],
                    team_a=f'Alpha{index}', team_b=f'Beta{index}', competition=source)
                self.assertEqual(200, bound.status_code, bound.text)
                self.assertTrue(bound.json()['resolved'])
                different = SOURCE_NAMES[index % len(SOURCE_NAMES)][0]
                rejected = await self.post('resolve_match', search_key=result['search_key'],
                    team_a=f'Alpha{index}', team_b=f'Beta{index}', competition=different)
                self.assertEqual(200, rejected.status_code)
                self.assertFalse(rejected.json()['resolved'])

    async def test_unknown_project_and_appended_division_names_do_not_expand(self):
        for index, (source, canonical) in enumerate(SOURCE_NAMES, 1):
            await self.fixture(index, competition_name=canonical)
            await self.fixture(index + 10, competition_name=canonical, sport='basketball',
                               home_team=f'Alpha{index}', away_team=f'Beta{index}')
            for sport, query in ((None, source), ('basketball', source),
                                  ('football', source + '青年'), ('football', source + '女子')):
                with self.subTest(source=source, sport=sport, query=query):
                    result = await self.search(team_query=f'Alpha{index}', sport=sport,
                                               competition_query=query)
                    self.assertEqual([], result['items'])
