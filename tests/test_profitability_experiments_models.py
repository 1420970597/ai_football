"""Boundary tests for the P04 Dixon-Coles baseline (no archives or network needed)."""

import math
import unittest
from datetime import date, timedelta

from infra.profitability.experiments.evidence import INSUFFICIENT
from infra.profitability.experiments.models import (
    MAX_GOALS, RHO_BOUND, _ll_and_grad, _tau, evaluate, fit, split_by_season,
)


def dataset():
    scores = [(2, 0), (0, 1), (1, 1), (3, 1), (0, 0), (1, 2), (2, 2), (4, 0)]
    teams = ['a', 'b', 'c']
    matches = []
    for index, (home_goals, away_goals) in enumerate(scores * 12):
        matches.append({
            'home': teams[index % 3],
            'away': teams[(index + 1) % 3],
            'home_goals': home_goals,
            'away_goals': away_goals,
            'season': '2526' if index >= 66 else '2425',
            'date': (date(2025, 1, 1) + timedelta(days=index)).isoformat(),
        })
    return matches


class GradientTest(unittest.TestCase):
    def test_analytic_gradient_matches_finite_difference(self):
        matches = dataset()[:40]
        attack = {'a': 0.15, 'b': -0.10, 'c': 0.05}
        defence = {'a': -0.08, 'b': 0.12, 'c': -0.04}
        home, rho = 0.22, -0.06
        result = _ll_and_grad(matches, attack, defence, home, rho)
        self.assertIsNotNone(result)
        _, g_attack, g_defence, g_home, g_rho = result
        eps = 1e-6

        def ll(a=attack, d=defence, h=home, r=rho):
            return _ll_and_grad(matches, a, d, h, r)[0]

        for team in attack:
            numeric = (ll(a={**attack, team: attack[team] + eps})
                       - ll(a={**attack, team: attack[team] - eps})) / (2 * eps)
            self.assertAlmostEqual(g_attack[team], numeric,
                                   delta=1e-4 * max(1.0, abs(g_attack[team])), msg=team)
        for team in defence:
            numeric = (ll(d={**defence, team: defence[team] + eps})
                       - ll(d={**defence, team: defence[team] - eps})) / (2 * eps)
            self.assertAlmostEqual(g_defence[team], numeric,
                                   delta=1e-4 * max(1.0, abs(g_defence[team])), msg=team)
        self.assertAlmostEqual(g_home, (ll(h=home + eps) - ll(h=home - eps)) / (2 * eps),
                               delta=1e-4 * max(1.0, abs(g_home)))
        self.assertAlmostEqual(g_rho, (ll(r=rho + eps) - ll(r=rho - eps)) / (2 * eps),
                               delta=1e-4 * max(1.0, abs(g_rho)))


class ConvergenceAndStabilityTest(unittest.TestCase):
    def test_fit_converges_and_is_deterministic(self):
        matches = dataset()
        zeros = dict.fromkeys(['a', 'b', 'c'], 0.0)
        initial = _ll_and_grad(matches, zeros, zeros, 0.0, 0.0)[0]
        first = fit(matches, max_iterations=500)
        self.assertTrue(first.converged)
        self.assertGreater(first.iterations, 0)
        self.assertLessEqual(first.iterations, 500)
        self.assertGreater(first.log_likelihood, initial)
        self.assertTrue(math.isfinite(first.log_likelihood))
        self.assertLessEqual(abs(first.rho), RHO_BOUND + 1e-12)
        second = fit(matches, max_iterations=500)
        self.assertEqual(first.log_likelihood, second.log_likelihood)
        self.assertEqual(first.iterations, second.iterations)
        self.assertEqual(first.home, second.home)
        self.assertEqual(first.rho, second.rho)

    def test_identifiability_and_parameter_variance_confinement(self):
        matches = dataset()
        model = fit(matches)
        # one constraint fixes the unidentifiable scale; defence carries the offset
        self.assertAlmostEqual(sum(model.attack.values()), 0.0, places=10)
        self.assertTrue(math.isfinite(sum(model.defence.values())))
        for values in (model.attack.values(), model.defence.values()):
            for value in values:
                self.assertTrue(math.isfinite(value))
                self.assertLess(abs(value), 3.0)
        # shifting attack by c and defence by -c leaves every scoring rate unchanged
        shifted = _ll_and_grad(
            matches,
            {k: v + 0.7 for k, v in model.attack.items()},
            {k: v - 0.7 for k, v in model.defence.items()},
            model.home, model.rho)[0]
        self.assertAlmostEqual(shifted, model.log_likelihood, places=8)


class LeakageTest(unittest.TestCase):
    def test_holdout_season_never_enters_the_fit(self):
        matches = dataset()
        train, test = split_by_season(matches, {'2526'})
        self.assertTrue(test)
        self.assertTrue(train)
        self.assertFalse({m['season'] for m in train} & {'2526'})
        future = dict(train[0], season='2627', date='2026-08-01')
        self.assertEqual(split_by_season([*matches, future], {'2526'}), (train, test))
        baseline = fit(train)
        mutated = [dict(match, home_goals=9, away_goals=0) if match in test else match
                   for match in matches]
        train_again, _ = split_by_season(mutated, {'2526'})
        self.assertEqual(train, train_again)
        rerun = fit(train_again)
        self.assertEqual(baseline.log_likelihood, rerun.log_likelihood)
        self.assertEqual(baseline.attack, rerun.attack)
        self.assertEqual(baseline.defence, rerun.defence)
        self.assertEqual(baseline.home, rerun.home)
        self.assertEqual(baseline.rho, rerun.rho)


class NormalisationAndBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = fit(dataset())

    def test_scoreline_grid_is_complete_and_normalised(self):
        grid = self.model.grid('a', 'b')
        self.assertEqual(len(grid), (MAX_GOALS + 1) ** 2)
        self.assertAlmostEqual(sum(grid.values()), 1.0, places=10)
        self.assertTrue(all(value >= 0 for value in grid.values()))
        for line in (0.5, 1.5, 2.5, 3.5):
            totals = self.model.totals_probabilities('a', 'b', line)
            self.assertAlmostEqual(totals['over'] + totals['under'], 1.0, places=10)
            self.assertGreaterEqual(totals['over'], 0.0)
            self.assertLessEqual(totals['over'], 1.0)
        outcomes = self.model.outcome_probabilities('a', 'b')
        self.assertAlmostEqual(sum(outcomes.values()), 1.0, places=10)
        for side in ('home', 'draw', 'away'):
            self.assertGreaterEqual(outcomes[side], 0.0)
            self.assertLessEqual(outcomes[side], 1.0)

    def test_tau_rejects_impossible_low_score_corrections(self):
        self.assertEqual(_tau(2, 2, 1.2, 0.8, -0.1), (1.0, 0.0, 0.0, 0.0))
        self.assertIsNone(_tau(0, 0, 2.0, 2.0, 0.5))
        self.assertIsNone(_tau(1, 1, 1.0, 1.0, 1.0))
        self.assertIsNone(_tau(0, 1, 2.0, 1.0, -0.5))

    def test_evaluate_reports_coverage_losses_without_imputation(self):
        unseen = evaluate(self.model, [{'home': 'z', 'away': 'b', 'home_goals': 1,
                                        'away_goals': 0, 'result': 'home', 'closing': {}}])
        self.assertEqual(unseen['evidence_label'], INSUFFICIENT)
        self.assertEqual(unseen['scored_matches'], 0)
        self.assertEqual(unseen['missing_reasons'], {'unseen_club': 1})
        match = {'home': 'a', 'away': 'b', 'home_goals': 2, 'away_goals': 0, 'result': 'home',
                 'closing': {'AvgC': {'home': 2.0, 'draw': 3.3, 'away': 4.0}}}
        scored = evaluate(self.model, [match])
        self.assertEqual(scored['scored_matches'], 1)
        self.assertEqual(scored['priced_matches'], 1)
        self.assertIsNotNone(scored['market_brier'])
        self.assertTrue(0.0 <= scored['model_brier'] <= 2.0)
        self.assertEqual(scored['missing_reasons'], {})
        for bucket in scored['calibration']:
            lower, upper = map(float, bucket['bin'].split('-'))
            self.assertLessEqual(lower, bucket['mean_predicted'])
            self.assertLessEqual(bucket['mean_predicted'], upper)


if __name__ == '__main__':
    unittest.main()
