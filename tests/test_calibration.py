#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.calibration 的单元测试（报告 §8.8）。"""

from __future__ import annotations

import math
import unittest

from core.calibration import (
    brier_score,
    build_calibration_report,
    clv,
    clv_mean,
    expected_calibration_error,
    log_growth_rate,
    max_drawdown,
    returns_from_equity,
)


class TestBrier(unittest.TestCase):
    def test_perfect_prediction(self):
        self.assertAlmostEqual(brier_score([1.0, 0.0], [1, 0]), 0.0, places=12)

    def test_worst_prediction(self):
        self.assertAlmostEqual(brier_score([0.0, 1.0], [1, 0]), 1.0, places=12)

    def test_uninformative_is_quarter(self):
        """二分类永久预测 0.5 → Brier = 0.25。"""
        self.assertAlmostEqual(brier_score([0.5, 0.5], [1, 0]), 0.25, places=12)

    def test_truthy_parsing_variants(self):
        for t in (1, 1.0, True, "1", "true", "win", "WON", "yes", "Y", "hit"):
            self.assertAlmostEqual(brier_score([1.0], [t]), 0.0, places=12)
        for f in (0, 0.0, False, "0", "false", "lose", "LOST", "no", "n", "miss"):
            self.assertAlmostEqual(brier_score([0.0], [f]), 0.0, places=12)

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            brier_score([], [])

    def test_length_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            brier_score([0.5], [1, 0])

    def test_bad_probability_rejected(self):
        with self.assertRaises(ValueError):
            brier_score([1.5], [1])

    def test_unparsable_truthy_rejected(self):
        with self.assertRaises(ValueError):
            brier_score([0.5], ["maybe"])

    def test_unsupported_type_rejected(self):
        with self.assertRaises(TypeError):
            brier_score([0.5], [object()])


class TestECE(unittest.TestCase):
    def test_well_calibrated_low_ece(self):
        """完美校准：conf == acc → ECE = 0。"""
        ece, bins = expected_calibration_error([1.0, 0.0, 1.0, 0.0],
                                               [1, 0, 1, 0], n_bins=2)
        self.assertAlmostEqual(ece, 0.0, places=12)

    def test_overconfident_high_ece(self):
        """报告 §8.8：置信 0.9 但准确率 0.5 → ECE 显著。"""
        ece, bins = expected_calibration_error([0.9, 0.9], [1, 0], n_bins=10)
        self.assertGreater(ece, 0.3)
        self.assertTrue(any(b["overconfident"] for b in bins))

    def test_bins_have_required_fields(self):
        _, bins = expected_calibration_error([0.15, 0.85], [0, 1], n_bins=10)
        for b in bins:
            for k in ("bin", "lo", "hi", "n", "confidence", "accuracy",
                      "gap", "overconfident"):
                self.assertIn(k, b)

    def test_probability_one_does_not_overflow(self):
        """p=1.0 必须落在最后一箱，不得越界。"""
        ece, bins = expected_calibration_error([1.0, 1.0], [1, 0], n_bins=10)
        self.assertTrue(math.isfinite(ece))
        self.assertEqual(bins[-1]["bin"], 9)

    def test_probability_zero(self):
        ece, bins = expected_calibration_error([0.0], [0], n_bins=10)
        self.assertAlmostEqual(ece, 0.0, places=12)

    def test_invalid_bins_rejected(self):
        with self.assertRaises(ValueError):
            expected_calibration_error([0.5], [1], n_bins=0)

    def test_ece_in_range(self):
        ece, _ = expected_calibration_error([0.1, 0.9, 0.5], [0, 1, 1], n_bins=5)
        self.assertGreaterEqual(ece, 0.0)
        self.assertLessEqual(ece, 1.0)


class TestCLV(unittest.TestCase):
    def test_positive_when_beat_close(self):
        self.assertAlmostEqual(clv(2.20, 2.10), 2.20 / 2.10 - 1, places=12)

    def test_zero_when_equal(self):
        self.assertAlmostEqual(clv(2.0, 2.0), 0.0, places=12)

    def test_negative_when_worse(self):
        self.assertLess(clv(1.90, 2.10), 0.0)

    def test_mean_of_pairs(self):
        m = clv_mean([(2.20, 2.10), (2.0, 2.0)])
        self.assertIsNotNone(m)
        assert m is not None  # 供类型检查器收窄
        self.assertAlmostEqual(m, (2.2 / 2.1 - 1) / 2, places=12)

    def test_mean_empty_is_none(self):
        self.assertIsNone(clv_mean([]))

    def test_invalid_odds_rejected(self):
        with self.assertRaises(ValueError):
            clv(1.0, 2.0)
        with self.assertRaises(ValueError):
            clv(2.0, 1.0)


class TestDrawdownAndGrowth(unittest.TestCase):
    def test_max_drawdown_basic(self):
        # 峰值 120 → 谷 90 → dd = 30/120 = 0.25
        self.assertAlmostEqual(max_drawdown([100, 120, 90, 110]), 0.25, places=12)

    def test_no_drawdown_when_monotonic(self):
        self.assertAlmostEqual(max_drawdown([100, 110, 120]), 0.0, places=12)

    def test_empty_is_zero(self):
        self.assertAlmostEqual(max_drawdown([]), 0.0, places=12)

    def test_negative_equity_rejected(self):
        with self.assertRaises(ValueError):
            max_drawdown([100, -10])

    def test_returns_from_equity(self):
        r = returns_from_equity([100, 110, 99])
        self.assertAlmostEqual(r[0], 0.1, places=12)
        self.assertAlmostEqual(r[1], -0.1, places=12)

    def test_returns_from_short_series(self):
        self.assertEqual(returns_from_equity([100]), ())
        self.assertEqual(returns_from_equity([]), ())

    def test_log_growth_positive(self):
        self.assertAlmostEqual(log_growth_rate([0.1, 0.1]), math.log(1.1),
                               places=12)

    def test_log_growth_empty_rejected(self):
        with self.assertRaises(ValueError):
            log_growth_rate([])

    def test_log_growth_total_loss_rejected(self):
        with self.assertRaises(ValueError):
            log_growth_rate([-1.0])


class TestBuildReport(unittest.TestCase):
    def test_full_report(self):
        rep = build_calibration_report(
            probs=[0.9, 0.8, 0.7, 0.6], outcomes=[1, 1, 0, 1],
            equity=[10000, 10200, 10100, 10500],
            clv_pairs=[(2.2, 2.1)])
        self.assertEqual(rep.n, 4)
        self.assertIsNotNone(rep.brier)
        self.assertIsNotNone(rep.ece)
        self.assertIsNotNone(rep.max_drawdown)
        self.assertIsNotNone(rep.clv_mean)
        self.assertTrue(len(rep.bins) > 0)

    def test_empty_samples_note(self):
        rep = build_calibration_report(probs=[], outcomes=[])
        self.assertEqual(rep.n, 0)
        self.assertIsNone(rep.brier)
        self.assertTrue(any("无有效样本" in x for x in rep.notes))

    def test_positive_clv_triggers_dual_role_note(self):
        """报告 §8.8：正 CLV 是双刃剑，必须提示。"""
        rep = build_calibration_report(probs=[0.6], outcomes=[1],
                                       clv_pairs=[(2.5, 2.0)])
        self.assertTrue(any("双刃剑" in x or "降限额" in x for x in rep.notes))

    def test_json_safe(self):
        import json
        rep = build_calibration_report(probs=[0.6, 0.4], outcomes=[1, 0])
        json.dumps(rep.as_dict())

    def test_length_mismatch_graceful(self):
        """probs 与 outcomes 长度不一致时应记录告警而非崩溃。"""
        rep = build_calibration_report(probs=[0.6, 0.4], outcomes=[1])
        self.assertIsNone(rep.brier)
        self.assertTrue(len(rep.notes) > 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
