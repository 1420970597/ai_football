from datetime import datetime, timedelta, timezone
import unittest

from app.review_candidates import source_fragments
from app.team_matching import team_name_matches


class SourceFragmentTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 11, 4, tzinfo=timezone.utc)
        self.end = self.start+timedelta(days=3)

    def test_original_fragments_are_bounded_and_do_not_become_identity_aliases(self):
        query = '卡洛斯卡萨雷斯农业'
        terms = source_fragments(query, self.start, self.end)
        self.assertIn('农业', terms)
        self.assertTrue(all(term in query for term in terms))
        self.assertLessEqual(len(terms), 12)
        self.assertFalse(team_name_matches(query, '阿根廷农业U20'))

    def test_missing_wide_or_invalid_time_range_does_not_expand(self):
        for start, end in ((None, self.end), (self.start, None),
                           (self.end, self.start), (self.start, self.start),
                           (self.start, self.start+timedelta(days=7, seconds=1))):
            with self.subTest(start=start, end=end):
                self.assertEqual((), source_fragments('卡洛斯卡萨雷斯农业', start, end))

    def test_club_prefix_in_original_source_is_recalled_without_identity_alias(self):
        for query in ('德瓦联合万丹', '德瓦联合'):
            with self.subTest(query=query):
                terms = source_fragments(query, self.start, self.end)
                self.assertIn('德瓦', terms)
                self.assertLessEqual(len(terms), 12)
                self.assertTrue(all(term in query for term in terms))
                self.assertFalse(team_name_matches(query, '德瓦FC'))

    def test_generic_club_prefix_does_not_expand_candidate_identity(self):
        terms = source_fragments('青年德瓦联合', self.start, self.end)
        self.assertNotIn('青年', terms)
        self.assertNotIn('联合', terms)

    def test_latin_short_chinese_and_generic_suffixes_are_not_generated(self):
        for query in ('Agropecuario U20', '农业', '埃斯卡', 'Alpha Beta'):
            self.assertEqual((), source_fragments(query, self.start, self.end))
        terms = source_fragments('里约青年足球俱乐部', self.start, self.end)
        self.assertFalse({'青年', '足球', '俱乐部', '俱乐', '乐部'} & set(terms))
