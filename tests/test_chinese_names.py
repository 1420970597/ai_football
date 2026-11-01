import unittest
from pathlib import Path

from app.chinese_names import fold_chinese, sql_chinese_fold
from app.team_matching import normalize_team_name, team_name_matches


class ChineseNameTests(unittest.TestCase):
    def test_real_russian_fixture_names_and_league_fold(self):
        self.assertEqual('莫斯科火车头', normalize_team_name('莫斯科火車頭'))
        self.assertTrue(team_name_matches('莫斯科斯巴達', '莫斯科斯巴达'))
        self.assertEqual(normalize_team_name('俄羅斯超級聯賽'), normalize_team_name('俄罗斯超级联赛'))
        self.assertEqual('sevilla', normalize_team_name('Sévilla'))

    def test_sql_fold_is_query_bounded_and_symmetric(self):
        for query in ('莫斯科火车头', '莫斯科火車頭'):
            source, target = sql_chinese_fold(query)
            table = str.maketrans(source, target)
            self.assertEqual('莫斯科火车头', '莫斯科火車頭'.translate(table))
            self.assertLess(len(source), 40)
        self.assertEqual(('', ''), sql_chinese_fold('Lokomotiv'))

    def test_context_dependent_characters_are_not_guessed(self):
        self.assertEqual('乾', fold_chinese('乾'))
        self.assertFalse(team_name_matches('莫斯科火車頭', '莫斯科火车头(女)'))
        self.assertFalse(team_name_matches('莫斯科火車頭', '莫斯科火车头U21'))
        self.assertFalse(team_name_matches('西維爾', '塞维利亚'))

    def test_dictionary_character_fold_is_idempotent(self):
        dictionary = Path(__file__).parents[1] / 'services/api/app/data/opencc/TSCharacters.txt'
        for line in dictionary.read_text().splitlines():
            if line and not line.startswith('#'):
                source = line.split('\t')[0]
                folded = fold_chinese(source)
                self.assertEqual(folded, fold_chinese(folded), source)
