import os
import unittest
from app.team_matching import team_name_matches
from app.competition_names import normalize_competition_name
import test_reviews as support


CASES = (
    ('中岸水手女足', '中央海岸水手学院(女)', '珀斯光荣女足', '珀斯光荣(女)', '澳女联', '澳大利亚女子甲级联赛'),
    ('福岛联队', '福岛联队', '相模原', 'SC相模原', '日本J3联赛', '日本J3联赛'),
    ('新泻天鹅', '新潟天鹅', '特格瓦嘉洛宫崎', '特格瓦嘉洛宫崎', '日本J2联赛', '日本J2联赛'),
    ('北九州向日葵', '北九州向日葵', '温泉群马', '群马草津温泉', '日职丙', '日本J3联赛'),
    ('北区', '高力北区', '香港足球会', '港会', '港超联杯', '香港联赛杯'),
    ('九龙城', '和富九龙城', '杰志', '杰志', '港超联杯', '香港联赛杯'),
)


class LiveSourceNameTests(unittest.TestCase):
    def test_verified_australian_championship_abbreviation_is_sport_scoped(self):
        self.assertEqual(normalize_competition_name('澳锦标', 'football'),
            normalize_competition_name('澳大利亚锦标赛', 'football'))
        self.assertNotEqual(normalize_competition_name('澳锦标', 'basketball'),
            normalize_competition_name('澳大利亚锦标赛', 'basketball'))
        self.assertNotEqual(normalize_competition_name('澳锦标女足', 'football'),
            normalize_competition_name('澳大利亚锦标赛', 'football'))

    def test_whole_names_keep_competition_sport_and_squads(self):
        for a, home, b, away, league, canonical in CASES:
            self.assertEqual(normalize_competition_name(league, 'football'),
                normalize_competition_name(canonical, 'football'))
            for source, native in ((a, home), (b, away)):
                with self.subTest(source=source):
                    self.assertTrue(team_name_matches(source, native, sport='football', competition=league))
                    for suffix in ('U20', '(女)', '后备'):
                        self.assertFalse(team_name_matches(source, native + suffix, sport='football', competition=canonical))
                    if source != native:
                        self.assertFalse(team_name_matches(source, native, sport='basketball', competition=canonical))
                        self.assertFalse(team_name_matches(source, native, sport='football', competition=canonical + '青年'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class LiveSourceSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def test_each_original_pair_resolves_on_first_complete_search(self):
        for i, (a, home, b, away, league, canonical) in enumerate(CASES, 1):
            await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2 WHERE id=$1',
                self.message, f'{league}\n{a} VS {b}\n全场大2.5')
            target = await self.fixture(i, home_team=home, away_team=away, competition_name=canonical)
            await self.fixture(10+i, home_team=home+'U20', away_team=away+'U20', competition_name=canonical)
            await self.fixture(20+i, home_team=home, away_team=away, competition_name=canonical+'青年')
            result = await self.post('search_matches', sport='football', team_query=a,
                team_a=a, team_b=b, competition_query=league)
            self.assertEqual(200, result.status_code, result.text)
            body = result.json()
            self.assertTrue(body['search_complete'])
            self.assertTrue(body['resolution']['resolved'], body)
            self.assertEqual(target, body['resolution']['fixture']['id'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
