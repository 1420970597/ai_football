import unittest
from core.replay import proper_scores, wilson


class ReplayTests(unittest.TestCase):
    def test_perfect_and_wrong_prediction(self):
        self.assertEqual(proper_scores([1, 0, 0], 0), (0, 0))
        self.assertEqual(proper_scores([1, 0, 0], 1)[0], 2)
        self.assertGreater(proper_scores([1, 0, 0], 1)[1], 20)

    def test_probability_validation(self):
        for values in ([.2, .2], [-.1, 1.1], [float('nan'), .5], []):
            with self.assertRaises(ValueError):
                proper_scores(values, 0)

    def test_interval_is_nontrivial_even_for_all_wins(self):
        low, high = wilson(10, 10)
        self.assertLess(low, .8)
        self.assertAlmostEqual(high, 1)
        self.assertGreater(wilson(100, 100)[0], low)
        for k, n in ((1, 0), (-1, 10), (11, 10)):
            with self.assertRaises(ValueError):
                wilson(k, n)
