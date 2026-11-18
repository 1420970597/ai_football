import os
import unittest
from app.team_matching import team_name_matches, with_verified_aliases
from app.competition_names import normalize_competition_name
import test_reviews as support


CASES = (
    ('欧帕尔利奥U20', '铁路工人PR U20', '帕尔梅拉斯青年队', '帕尔梅拉斯U20', '巴青杯', '巴西杯U20'),
    ('广岛三箭里贾纳(女)', '广岛三箭(女)', '野岛斯特拉神奈川相模原(女)',
     '野岛斯特拉神奈川相模原(女)', '日本女子职业联赛', '日本女子职业联赛'),
    ('中岸水手女足', '中央海岸水手学院(女)', '珀斯光荣女足', '珀斯光荣(女)', '澳女联', '澳大利亚女子甲级联赛'),
    ('福岛联队', '福岛联队', '相模原', 'SC相模原', '日本J3联赛', '日本J3联赛'),
    ('新泻天鹅', '新潟天鹅', '特格瓦嘉洛宫崎', '特格瓦嘉洛宫崎', '日本J2联赛', '日本J2联赛'),
    ('北九州向日葵', '北九州向日葵', '温泉群马', '群马草津温泉', '日职丙', '日本J3联赛'),
    ('北区', '高力北区', '香港足球会', '港会', '港超联杯', '香港联赛杯'),
    ('九龙城', '和富九龙城', '杰志', '杰志', '港超联杯', '香港联赛杯'),
)


class LiveSourceNameTests(unittest.TestCase):
    def test_brazil_under_20_cup_scope_preserves_sport_and_age(self):
        self.assertEqual(normalize_competition_name('巴青杯', 'football'),
                         normalize_competition_name('巴西杯U20', 'football'))
        self.assertNotEqual(normalize_competition_name('巴青杯', 'basketball'),
                            normalize_competition_name('巴西杯U20', 'basketball'))
        for native in ('帕尔梅拉斯', '帕尔梅拉斯U17', '帕尔梅拉斯U23'):
            self.assertFalse(team_name_matches('帕尔梅拉斯青年队', native,
                                              sport='football', competition='巴西杯U20'))

    def test_hiroshima_alias_requires_the_womens_league_and_full_squad_label(self):
        source = '广岛三箭里贾纳(女)'
        native = '广岛三箭(女)'
        league = '日本女子职业联赛'
        self.assertTrue(team_name_matches(source, native, sport='football', competition=league))
        self.assertFalse(team_name_matches(source, '广岛三箭', sport='football', competition=league))
        for competition in (None, '日本职业联赛', '日本女子联赛杯'):
            self.assertFalse(team_name_matches(source, native, sport='football', competition=competition))

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
                        other_competition = canonical + '青年'
                        self.assertNotIn('verified_alias_sources', with_verified_aliases({
                            'sport': 'football', 'competition_name': other_competition, 'home_team': native,
                        }))
                        # The Perth pair also matches via the general women's suffix normalization.
                        self.assertEqual(source == '珀斯光荣女足', team_name_matches(
                            source, native, sport='football', competition=other_competition))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class LiveSourceSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture

    async def test_finished_brazil_cup_image_identifies_fixture_with_no_realtime_eligibility(self):
        team_a, home, team_b, away, league, canonical = CASES[0]
        await self.pool.execute(
            "UPDATE v2_expert_messages SET raw_text='参考截图',media_objects=$2 WHERE id=$1",
            self.message, [{'mime_type': 'image/png'}])
        fixture = await self.fixture(1, home_team=home, away_team=away,
                                     competition_name=canonical, phase='finished', markets=[])
        response = await self.post('search_matches', sport='football', team_query=team_a,
                                   team_a=team_a, team_b=team_b, competition_query=league)
        self.assertEqual(200, response.status_code, response.text)
        resolution = response.json()['resolution']
        self.assertTrue(resolution['resolved'], resolution)
        candidate = resolution['fixture']
        self.assertEqual(fixture, candidate['id'])
        self.assertFalse(candidate['realtime_eligible'])
        self.assertEqual('finished', candidate['eligibility_reason'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

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
