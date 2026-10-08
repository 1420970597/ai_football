#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.economics 的单元测试（报告 §8.2–§8.7）。

数值断言直接对照报告中的表，确保代码与文档一致。
"""

from __future__ import annotations

import math
import unittest

from core.economics import (
    breakeven_bets,
    build_covariance,
    edge_from_probabilities,
    effective_ev,
    expected_value,
    expected_value_from_margin,
    fractional_kelly,
    implied_probabilities,
    kelly_fraction,
    log_growth_rate,
    multivariate_kelly,
    naive_overbet_factor,
    portfolio_std,
    q_fill_model,
    ruin_probability_bound,
    shrink_probabilities,
    sizing,
    solve_linear_system,
)


class TestImpliedAndEV(unittest.TestCase):
    def test_implied_probabilities(self):
        q = implied_probabilities((2.0, 4.0))
        self.assertAlmostEqual(q[0], 0.5, places=12)
        self.assertAlmostEqual(q[1], 0.25, places=12)

    def test_implied_rejects_odds_le_one(self):
        with self.assertRaises(ValueError):
            implied_probabilities((1.0, 2.0))

    def test_expected_value_basic(self):
        # p=0.5, o=2.0 → EV = 0
        self.assertAlmostEqual(expected_value(0.5, 2.0), 0.0, places=12)
        # p=0.6, o=2.0 → EV = +0.2
        self.assertAlmostEqual(expected_value(0.6, 2.0), 0.2, places=12)

    def test_expected_value_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            expected_value(1.5, 2.0)
        with self.assertRaises(ValueError):
            expected_value(0.5, 1.0)

    def test_ev_from_margin_matches_report(self):
        """报告 §9.1：m=0.114 → EV = −10.23%。"""
        self.assertAlmostEqual(expected_value_from_margin(0.114),
                               -0.10233, places=5)
        self.assertAlmostEqual(expected_value_from_margin(0.05),
                               -0.047619, places=5)
        self.assertAlmostEqual(expected_value_from_margin(0.228),
                               -0.185667, places=5)

    def test_ev_from_margin_zero_is_zero(self):
        self.assertAlmostEqual(expected_value_from_margin(0.0), 0.0, places=12)

    def test_ev_from_margin_rejects_le_minus_one(self):
        with self.assertRaises(ValueError):
            expected_value_from_margin(-1.0)

    def test_edge_from_probabilities(self):
        e = edge_from_probabilities((0.5, 0.25), (2.0, 4.0))
        self.assertAlmostEqual(e[0], 0.0, places=12)
        self.assertAlmostEqual(e[1], 0.0, places=12)

    def test_edge_length_mismatch(self):
        with self.assertRaises(ValueError):
            edge_from_probabilities((0.5,), (2.0, 4.0))


class TestShrinkage(unittest.TestCase):
    def test_full_weight_when_no_uncertainty(self):
        """k=0 → w=1，完全不收缩。"""
        p, w = shrink_probabilities((0.6, 0.4), (0.5, 0.5), n=100, k=0.0)
        self.assertAlmostEqual(w, 1.0, places=12)
        self.assertAlmostEqual(p[0], 0.6, places=12)

    def test_zero_samples_shrinks_to_prior(self):
        """n=0 且 k>0 → w=0，完全采用先验。"""
        p, w = shrink_probabilities((0.9, 0.1), (0.5, 0.5), n=0, k=10.0)
        self.assertAlmostEqual(w, 0.0, places=12)
        self.assertAlmostEqual(p[0], 0.5, places=12)

    def test_partial_shrinkage_between(self):
        p, w = shrink_probabilities((0.8, 0.2), (0.5, 0.5), n=10, k=10.0)
        self.assertAlmostEqual(w, 0.5, places=12)
        self.assertAlmostEqual(p[0], 0.65, places=12)

    def test_k_from_sigma(self):
        """k = p(1−p)/σ² − 1。"""
        p, w = shrink_probabilities((0.5, 0.5), (0.5, 0.5), n=10,
                                    p_for_k=0.5, sigma_p=0.1)
        # k = 0.25/0.01 - 1 = 24 → w = 10/34
        self.assertAlmostEqual(w, 10 / 34, places=9)

    def test_length_mismatch(self):
        with self.assertRaises(ValueError):
            shrink_probabilities((0.5,), (0.5, 0.5), n=1)

    def test_negative_n_rejected(self):
        with self.assertRaises(ValueError):
            shrink_probabilities((0.5,), (0.5,), n=-1)

    def test_invalid_sigma_rejected(self):
        with self.assertRaises(ValueError):
            shrink_probabilities((0.5,), (0.5,), n=1, p_for_k=0.5, sigma_p=0.0)


class TestEffectiveEV(unittest.TestCase):
    """报告 §8.3：q_fill 对 EV 与注额均为负偏导。"""

    def test_q_fill_decreases_with_ev(self):
        vals = [q_fill_model(ev, 100.0) for ev in (0.0, 0.02, 0.05, 0.10)]
        for a, b in zip(vals, vals[1:]):
            self.assertGreater(a, b)

    def test_q_fill_decreases_with_stake(self):
        vals = [q_fill_model(0.02, s) for s in (50, 100, 500, 2000)]
        for a, b in zip(vals, vals[1:]):
            self.assertGreaterEqual(a, b)

    def test_q_fill_in_range(self):
        for ev in (-0.1, 0.0, 0.5, 2.0):
            for s in (0, 100, 1e9):
                q = q_fill_model(ev, s)
                self.assertGreaterEqual(q, 0.0)
                self.assertLessEqual(q, 1.0)

    def test_negative_stake_rejected(self):
        with self.assertRaises(ValueError):
            q_fill_model(0.0, -1.0)

    def test_effective_ev(self):
        self.assertAlmostEqual(effective_ev(0.10, 0.9, 0.001),
                               0.9 * 0.10 - 0.001, places=12)


class TestKelly(unittest.TestCase):
    def test_kelly_zero_at_fair_odds(self):
        """p·o = 1 时 f* = 0。"""
        self.assertAlmostEqual(kelly_fraction(0.5, 2.0), 0.0, places=12)

    def test_kelly_positive_with_edge(self):
        self.assertAlmostEqual(kelly_fraction(0.55, 2.0), 0.1, places=12)

    def test_kelly_negative_without_edge(self):
        self.assertLess(kelly_fraction(0.45, 2.0), 0.0)

    def test_kelly_formula_matches_manual(self):
        p, o = 0.6, 2.5
        b = o - 1
        expect = (b * p - (1 - p)) / b
        self.assertAlmostEqual(kelly_fraction(p, o), expect, places=12)

    def test_fractional_kelly_scales(self):
        self.assertAlmostEqual(fractional_kelly(0.55, 2.0, 0.5), 0.05, places=12)

    def test_fractional_kelly_negative_lam_rejected(self):
        with self.assertRaises(ValueError):
            fractional_kelly(0.55, 2.0, -0.1)

    def test_naive_overbet_matches_report(self):
        """报告 §8.5：ρ=0.2→1.25×，ρ=0.4→1.67×，ρ=0.6→2.50×。"""
        self.assertAlmostEqual(naive_overbet_factor(0.2), 1.25, places=6)
        self.assertAlmostEqual(naive_overbet_factor(0.4), 1 / 0.6, places=9)
        self.assertAlmostEqual(naive_overbet_factor(0.6), 2.5, places=6)

    def test_naive_overbet_zero_for_nonpositive_rho(self):
        self.assertAlmostEqual(naive_overbet_factor(0.0), 1.0, places=12)
        self.assertAlmostEqual(naive_overbet_factor(-0.5), 1.0, places=12)

    def test_naive_overbet_rho_one_rejected(self):
        with self.assertRaises(ValueError):
            naive_overbet_factor(1.0)


class TestLinearSolver(unittest.TestCase):
    """高斯消元（多元 Kelly 的基础）。"""

    def test_identity(self):
        x = solve_linear_system([[1.0, 0.0], [0.0, 1.0]], [3.0, 4.0])
        self.assertAlmostEqual(x[0], 3.0, places=12)
        self.assertAlmostEqual(x[1], 4.0, places=12)

    def test_diagonal(self):
        x = solve_linear_system([[2.0, 0.0], [0.0, 4.0]], [1.0, 2.0])
        self.assertAlmostEqual(x[0], 0.5, places=12)
        self.assertAlmostEqual(x[1], 0.5, places=12)

    def test_needs_pivoting(self):
        """首元为 0 时必须换行，否则除零。"""
        x = solve_linear_system([[0.0, 1.0], [1.0, 0.0]], [2.0, 3.0])
        self.assertAlmostEqual(x[0], 3.0, places=12)
        self.assertAlmostEqual(x[1], 2.0, places=12)

    def test_known_system(self):
        # 2x + y = 5 ; x + 3y = 10  → x=1, y=3
        x = solve_linear_system([[2.0, 1.0], [1.0, 3.0]], [5.0, 10.0])
        self.assertAlmostEqual(x[0], 1.0, places=9)
        self.assertAlmostEqual(x[1], 3.0, places=9)

    def test_singular_rejected(self):
        with self.assertRaises(ValueError):
            solve_linear_system([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0])

    def test_dimension_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            solve_linear_system([[1.0, 0.0]], [1.0, 2.0])
        with self.assertRaises(ValueError):
            solve_linear_system([], [])


class TestMultivariateKelly(unittest.TestCase):
    def test_diagonal_covariance_matches_analytic(self):
        """对角协方差下 f* = λ·μ_i/σ_ii。"""
        f = multivariate_kelly((0.05, 0.03), ((0.10, 0.0), (0.0, 0.06)), lam=1.0)
        self.assertAlmostEqual(f[0], 0.5, places=9)
        self.assertAlmostEqual(f[1], 0.5, places=9)

    def test_lambda_scales_result(self):
        f1 = multivariate_kelly((0.05,), ((0.10,),), lam=1.0)
        f2 = multivariate_kelly((0.05,), ((0.10,),), lam=0.5)
        self.assertAlmostEqual(f2[0], f1[0] * 0.5, places=12)

    def test_correlation_reduces_total_position(self):
        """正相关应降低总仓位（相关性惩罚）。"""
        mu = (0.05, 0.05)
        indep = multivariate_kelly(mu, ((0.10, 0.0), (0.0, 0.10)), lam=1.0)
        corr = multivariate_kelly(mu, ((0.10, 0.08), (0.08, 0.10)), lam=1.0)
        self.assertGreater(sum(indep), sum(corr))

    def test_dimension_mismatch(self):
        with self.assertRaises(ValueError):
            multivariate_kelly((0.05,), ((0.1, 0.0), (0.0, 0.1)))


class TestCovariance(unittest.TestCase):
    def test_diagonal_only_when_rho_zero(self):
        cov = build_covariance((0.1, 0.1), (0.5, 0.5), (2.0, 2.0), rho=0.0)
        self.assertAlmostEqual(cov[0][1], 0.0, places=12)
        self.assertGreater(cov[0][0], 0.0)

    def test_offdiagonal_positive_with_rho(self):
        cov = build_covariance((0.1, 0.1), (0.5, 0.5), (2.0, 2.0), rho=0.4)
        self.assertGreater(cov[0][1], 0.0)
        self.assertAlmostEqual(cov[0][1], cov[1][0], places=12)

    def test_portfolio_std_increases_with_rho(self):
        a = portfolio_std(build_covariance((0.1, 0.1), (0.5, 0.5), (2.0, 2.0), 0.0))
        b = portfolio_std(build_covariance((0.1, 0.1), (0.5, 0.5), (2.0, 2.0), 0.6))
        self.assertGreater(b, a)

    def test_length_mismatch(self):
        with self.assertRaises(ValueError):
            build_covariance((0.1,), (0.5, 0.5), (2.0, 2.0))


class TestSizing(unittest.TestCase):
    def test_zero_when_no_edge(self):
        r = sizing(("a", "b"), (0.5, 0.5), (2.0, 2.0), (-0.1, -0.1))
        self.assertAlmostEqual(r.total_exposure, 0.0, places=12)

    def test_positive_with_edge(self):
        r = sizing(("a",), (0.6,), (2.0,), (0.2,), lam=1.0)
        self.assertGreater(r.fractions[0], 0.0)
        self.assertFalse(r.used_covariance)

    def test_exposure_cap_respected(self):
        r = sizing(("a",), (0.9,), (5.0,), (3.5,), lam=1.0,
                   max_total_exposure=0.05)
        self.assertLessEqual(r.total_exposure, 0.05 + 1e-12)

    def test_covariance_used_when_rho_positive(self):
        r = sizing(("a", "b"), (0.6, 0.6), (2.0, 2.0), (0.2, 0.2), rho=0.4)
        self.assertTrue(r.used_covariance)
        self.assertAlmostEqual(r.correlation_penalty, 1 / 0.6, places=9)

    def test_rho_zero_warns_about_optimism(self):
        r = sizing(("a",), (0.6,), (2.0,), (0.2,))
        self.assertTrue(any("偏乐观" in n or "相关系" in n for n in r.notes))

    def test_min_edge_filters(self):
        r = sizing(("a", "b"), (0.6, 0.6), (2.0, 2.0), (0.2, 0.01), min_edge=0.05)
        self.assertGreater(r.fractions[0], 0.0)
        self.assertAlmostEqual(r.fractions[1], 0.0, places=12)

    def test_invalid_rho_rejected(self):
        with self.assertRaises(ValueError):
            sizing(("a",), (0.6,), (2.0,), (0.2,), rho=1.0)

    def test_invalid_exposure_rejected(self):
        with self.assertRaises(ValueError):
            sizing(("a",), (0.6,), (2.0,), (0.2,), max_total_exposure=0.0)
        with self.assertRaises(ValueError):
            sizing(("a",), (0.6,), (2.0,), (0.2,), max_total_exposure=1.5)


class TestBreakevenAndRisk(unittest.TestCase):
    def test_breakeven_matches_report(self):
        """报告 §8.7 表：$35.3/天、+2%、$100 → 18 注/天。"""
        self.assertAlmostEqual(breakeven_bets(35.3, 0.02, 100.0), 17.65, places=1)
        self.assertAlmostEqual(breakeven_bets(35.3, 0.02, 10.0), 176.5, places=1)

    def test_breakeven_infinite_without_edge(self):
        self.assertEqual(breakeven_bets(35.3, 0.0, 100.0), math.inf)
        self.assertEqual(breakeven_bets(35.3, -0.01, 100.0), math.inf)

    def test_breakeven_rejects_bad_stake(self):
        with self.assertRaises(ValueError):
            breakeven_bets(35.3, 0.02, 0.0)
        with self.assertRaises(ValueError):
            breakeven_bets(-1.0, 0.02, 100.0)

    def test_breakeven_death_spiral(self):
        """报告 §8.7：被拒单（q_fill↓）会推高所需注数。"""
        full = breakeven_bets(35.3, 0.02, 100.0, q_fill=1.0)
        half = breakeven_bets(35.3, 0.02, 100.0, q_fill=0.5)
        self.assertAlmostEqual(half, full * 2, places=6)

    def test_log_growth_concave_around_kelly(self):
        """报告 §8.6：g 在 f* 处最大，超过后下降。"""
        p, o = 0.55, 2.0
        f_star = kelly_fraction(p, o)
        def g(f: float) -> float:
            return p * math.log(1 + f * (o - 1)) + (1 - p) * math.log(1 - f)
        self.assertGreater(g(f_star), g(f_star * 0.5))
        self.assertGreater(g(f_star), g(min(f_star * 2, 0.9)))

    def test_log_growth_empty_rejected(self):
        with self.assertRaises(ValueError):
            log_growth_rate([])

    def test_log_growth_total_loss_rejected(self):
        with self.assertRaises(ValueError):
            log_growth_rate([-1.0])

    def test_ruin_probability_monotonic_in_stake(self):
        """破产概率必须随下注比例单调递增。"""
        # 显式 float() 归一：让断言不依赖函数返回标注的宽窄，
        # 也覆盖 Decimal / numpy 标量等非原生数值。
        vals = [float(ruin_probability_bound(10000, s))
                for s in (50, 500, 2000, 5000, 9000)]
        for a, b in zip(vals, vals[1:]):
            self.assertLessEqual(a, b + 1e-12)

    def test_ruin_probability_bounds(self):
        self.assertEqual(float(ruin_probability_bound(10000, 0)), 0.0)
        self.assertEqual(float(ruin_probability_bound(10000, 10000)), 1.0)
        self.assertEqual(float(ruin_probability_bound(10000, 20000)), 1.0)

    def test_ruin_probability_rejects_bad_bankroll(self):
        with self.assertRaises(ValueError):
            ruin_probability_bound(0, 100)
        with self.assertRaises(ValueError):
            ruin_probability_bound(1000, -1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
