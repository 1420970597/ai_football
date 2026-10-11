import unittest

from core.ensemble import cohere_candidates


class EnsembleCoherenceTests(unittest.TestCase):
    def test_complete_had_is_normalized_but_partial_quotes_are_untouched(self):
        complete = [{'market': 'HAD', 'line': '', 'outcome': outcome, 'p_model': value}
                    for outcome, value in (('home', .7), ('draw', .2), ('away', .2))]
        partial = [{'market': 'OU', 'line': '2.5', 'outcome': 'over', 'p_model': .7}]
        cohere_candidates(complete + partial)
        self.assertAlmostEqual(sum(c['p_model'] for c in complete), 1, places=6)
        self.assertNotIn('probability_sum', partial[0])

    def test_half_market_groups_are_coherent_by_family(self):
        candidates = [{'market': 'AH_1H', 'line': '-0.5', 'outcome': outcome, 'p_model': .5}
                      for outcome in ('home', 'away')]
        cohere_candidates(candidates)
        self.assertEqual(sum(c['p_model'] for c in candidates), 1)


if __name__ == '__main__':
    unittest.main()
