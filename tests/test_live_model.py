import unittest

from core.live_model import (clock_seconds, competition_type, fit_share, fit_total,
                             payment, remaining_distribution)
from core.settlement import pnl_for, settle_pick


class LiveModelTests(unittest.TestCase):
    def test_time_is_seconds_and_ambiguous_second_half_is_rejected(self):
        self.assertEqual(clock_seconds('2812', '7'), 2812)
        self.assertEqual(clock_seconds('45:00', '6'), 2700)
        self.assertIsNone(clock_seconds('300', '7'))
        self.assertIsNone(clock_seconds('nan', '6'))
        self.assertIsNone(clock_seconds(True, '6'))

    def test_virtual_and_real_are_separate(self):
        self.assertEqual(competition_type({'league': 'EAFC Battle'}), 'virtual')
        self.assertEqual(competition_type({'league': '英超'}), 'real')
        self.assertEqual(competition_type({}), 'unknown')

    def test_payment_matches_all_asian_settlements(self):
        dist = remaining_distribution(.7, .5, (1, 0))
        for family, outcomes in (('OU', ('over', 'under')), ('AH', ('home', 'away'))):
            for line in (-.75, -.25, 0, .25, 2.25, 2.75):
                for outcome in outcomes:
                    model = payment(dist, family, outcome, line)
                    settled = sum(p * pnl_for(settle_pick(family, outcome, line, (h, a))[0], 1.93)
                                  for h, a, p in dist)
                    self.assertAlmostEqual(model.ev(1.93), settled, places=9)

    def test_score_conditioning_not_future_score(self):
        at_start = payment(remaining_distribution(1, 1, (0, 0)), 'HAD', 'home').win
        leading = payment(remaining_distribution(.1, .1, (1, 0)), 'HAD', 'home').win
        self.assertGreater(leading, at_start)
        self.assertGreater(leading, .90)

    def test_total_inverse_and_home_share(self):
        dist = remaining_distribution(1.2, .8, (1, 0))
        p = payment(dist, 'OU', 'over', 2.25).effective_probability
        total = fit_total(2.25, p, 1)
        self.assertAlmostEqual(total, 2, places=3)
        home = payment(dist, 'HAD', 'home').win
        away = payment(dist, 'HAD', 'away').win
        fitted_h, fitted_a = fit_share(total, (1, 0), home, away)
        self.assertAlmostEqual(fitted_h, 1.2, places=2)
        self.assertAlmostEqual(fitted_a, .8, places=2)

    def test_invalid_distribution_and_market(self):
        with self.assertRaises(ValueError):
            remaining_distribution(float('nan'), 1, (0, 0))
        with self.assertRaises(ValueError):
            payment([(0, 0, 1)], 'OU', 'home', 2.25)
        self.assertIsNone(fit_total('bad', .5, 0))

    def test_zero_goals_mass_and_tail_exposed(self):
        dist = remaining_distribution(0, 0, (2, 1))
        self.assertEqual(dist, ((2, 1, 1.0),))
        result = payment(dist, 'OU', 'over', 3.25)
        self.assertEqual(result.loss, .5)
        self.assertEqual(result.push, .5)
