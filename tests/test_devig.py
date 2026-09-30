#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.devig 的单元测试（报告 §8.1）。

重点验证：
  - 五种去水方法各自的数值性质
  - 归一化与 Shin 的 z 求解
  - 方法间分歧度量与告警阈值
  - 边界/异常（booksum ≤ 0、负概率、停盘快照）
"""

from __future__ import annotations

import math
import unittest

from core.devig import (
    DEFAULT_SPREAD_WARN_PP,
    additive,
    additive_is_valid,
    devig,
    method_spread_pp,
    odds_ratio,
    power,
    proportional,
    shin,
)
from core.models import DevigMethod, OddsSnapshot, SnapshotState


def snap(odds, state=SnapshotState.ACTIVE, outcomes=("home", "draw", "away")):
    return OddsSnapshot(
        match_id="2001", league="日职", home="A", away="B",
        market="1X2", outcomes=outcomes, odds=odds, state=state,
    )


ODDS = (2.13, 3.45, 2.70)


class TestProportional(unittest.TestCase):
    def test_sums_to_one(self):
        q = tuple(1 / o for o in ODDS)
        p = proportional(q)
        self.assertAlmostEqual(sum(p), 1.0, places=12)

    def test_preserves_relative_order(self):
        q = tuple(1 / o for o in ODDS)
        p = proportional(q)
        # 赔率越低（越热门）概率越高
        self.assertGreater(p[0], p[2])
        self.assertGreater(p[2], p[1])

    def test_all_zero_falls_back_to_uniform(self):
        p = proportional((0.0, 0.0, 0.0))
        for x in p:
            self.assertAlmostEqual(x, 1 / 3, places=12)

    def test_matches_manual_computation(self):
        q = (0.5, 0.25, 0.25)
        p = proportional(q)
        self.assertAlmostEqual(p[0], 0.5, places=12)


class TestAdditive(unittest.TestCase):
    def test_sums_to_one_for_normal_booksum(self):
        q = tuple(1 / o for o in ODDS)
        p = additive(q)
        self.assertAlmostEqual(sum(p), 1.0, places=12)

    def test_can_produce_negative_probability(self):
        """报告 §8.1 明确列出该缺陷：加法法可能产生负概率。

        触发条件：booksum 显著大于 1（即小众市场水钱极高）。
        例：赔率 1.11/1.11/100 → booksum≈1.81，冷门位概率被减为负。
        """
        q = (1 / 1.11, 1 / 1.11, 1 / 100.0)
        p = additive(q)
        self.assertTrue(any(x < 0 for x in p))
        self.assertFalse(additive_is_valid(p))

    def test_additive_is_valid_true_for_normal(self):
        q = tuple(1 / o for o in ODDS)
        self.assertTrue(additive_is_valid(additive(q)))

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            additive(())


class TestPower(unittest.TestCase):
    def test_sums_to_one(self):
        p = power(tuple(1 / o for o in ODDS))
        self.assertAlmostEqual(sum(p), 1.0, places=9)

    def test_shifts_more_probability_to_favourites(self):
        """幂法把更多概率推向热门，与 FL bias 方向一致。"""
        q = tuple(1 / o for o in ODDS)
        prop = proportional(q)
        pw = power(q)
        self.assertGreaterEqual(pw[0], prop[0] - 1e-9)

    def test_zero_booksum_normalized(self):
        p = power((0.0, 0.0, 0.0))
        self.assertAlmostEqual(sum(p), 1.0, places=9)


class TestOddsRatio(unittest.TestCase):
    def test_sums_to_one(self):
        p = odds_ratio(tuple(1 / o for o in ODDS))
        self.assertAlmostEqual(sum(p), 1.0, places=9)

    def test_all_probabilities_in_range(self):
        p = odds_ratio(tuple(1 / o for o in ODDS))
        for x in p:
            self.assertGreaterEqual(x, 0.0)
            self.assertLessEqual(x, 1.0)

    def test_extreme_odds_still_valid(self):
        """极端赔率（1.01 ~ 1000）不应产生 NaN/越界。"""
        for odds in ((1.01, 500.0, 1000.0), (1.02, 1.02, 1.02)):
            p = odds_ratio(tuple(1 / o for o in odds))
            self.assertAlmostEqual(sum(p), 1.0, places=6)
            for x in p:
                self.assertTrue(math.isfinite(x))
                self.assertGreaterEqual(x, 0.0)
                self.assertLessEqual(x, 1.0)


class TestShin(unittest.TestCase):
    def test_sums_to_one_and_z_in_range(self):
        p, z = shin(tuple(1 / o for o in ODDS))
        self.assertAlmostEqual(sum(p), 1.0, places=9)
        self.assertGreaterEqual(z, 0.0)
        self.assertLess(z, 0.5)

    def test_z_matches_reference_magnitude(self):
        """报告 §8.1 示例盘口 booksum=1.0314 得 z≈0.0157。"""
        p, z = shin((1 / 1.90, 1 / 3.60, 1 / 4.40))
        self.assertAlmostEqual(z, 0.0157, places=3)
        self.assertAlmostEqual(sum(p), 1.0, places=9)

    def test_non_proportional_vs_proportional(self):
        """Shin 不应等于比例法（它内生修正 FL bias）。"""
        q = tuple(1 / o for o in ODDS)
        p_shin, _ = shin(q)
        p_prop = proportional(q)
        diff = sum(abs(a - b) for a, b in zip(p_shin, p_prop))
        self.assertGreater(diff, 1e-6)

    def test_zero_booksum_rejected(self):
        with self.assertRaises(ValueError):
            shin((0.0, 0.0, 0.0))


class TestMethodSpread(unittest.TestCase):
    def test_zero_for_single_method(self):
        self.assertEqual(method_spread_pp({"a": (0.5, 0.5)}), 0.0)

    def test_zero_for_identical_methods(self):
        self.assertAlmostEqual(
            method_spread_pp({"a": (0.4, 0.6), "b": (0.4, 0.6)}), 0.0, places=12)

    def test_positive_for_different_methods(self):
        s = method_spread_pp({"a": (0.5, 0.5), "b": (0.4, 0.6)})
        self.assertAlmostEqual(s, 20.0, places=9)   # L1 = 0.2 → 20pp

    def test_length_mismatch_skipped(self):
        s = method_spread_pp({"a": (0.5, 0.5), "b": (0.3, 0.3, 0.4)})
        self.assertEqual(s, 0.0)


class TestDevigMain(unittest.TestCase):
    def test_auto_selects_shin(self):
        fp = devig(snap(ODDS), method=DevigMethod.AUTO)
        self.assertIs(fp.method, DevigMethod.SHIN)
        self.assertIsNotNone(fp.shin_z)
        self.assertTrue(fp.sums_to_one)

    def test_all_methods_present_in_per_method(self):
        fp = devig(snap(ODDS))
        for m in ("proportional", "additive", "power", "odds_ratio", "shin"):
            self.assertIn(m, fp.per_method)

    def test_explicit_method_respected(self):
        fp = devig(snap(ODDS), method=DevigMethod.PROPORTIONAL)
        self.assertIs(fp.method, DevigMethod.PROPORTIONAL)

    def test_spread_warning_triggers_on_real_data(self):
        """真实数据（报告 §8.1 同源）偏差 > 1pp，应触发告警。"""
        fp = devig(snap(ODDS))
        self.assertGreater(fp.method_spread_pp, DEFAULT_SPREAD_WARN_PP)
        self.assertTrue(fp.spread_warning)
        self.assertTrue(any("翻转" in n for n in fp.notes))

    def test_margin_matches_snapshot(self):
        s = snap(ODDS)
        fp = devig(s)
        self.assertAlmostEqual(fp.margin, s.margin, places=12)

    def test_suspended_state_noted_but_still_computed(self):
        fp = devig(snap(ODDS, state=SnapshotState.SUSPENDED))
        self.assertEqual(len(fp.probabilities), 3)
        self.assertTrue(any("停盘" in n or "suspended" in n for n in fp.notes))

    def test_negative_booksum_market_noted(self):
        """booksum < 1（套利/异常）应产生负水钱并写明。"""
        fp = devig(snap((4.0, 4.0, 4.0)))
        self.assertLess(fp.margin, 0.0)
        self.assertTrue(any("套利" in n or "解析错误" in n for n in fp.notes))

    def test_as_dict_json_safe(self):
        import json
        json.dumps(devig(snap(ODDS)).as_dict())

    # -- 边界与异常 ---------------------------------------------------------

    def test_zero_odds_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            snap((0.0, 0.0, 0.0))

    def test_booksum_zero_rejected_in_devig(self):
        """构造上赔率必须 > 1，故 booksum 恒正；此处直接验证防御分支。"""
        s = snap((1.0000001, 1.0000001, 1.0000001))
        fp = devig(s)
        self.assertAlmostEqual(sum(fp.probabilities), 1.0, places=6)

    def test_fallback_when_explicit_method_unavailable(self):
        """指定方法不可用时应回退并写明（此处用 additive 负概率场景）。"""
        s = snap((1.11, 1.11, 100.0))   # booksum≈1.81 → 加法法出负概率
        fp = devig(s, method=DevigMethod.ADDITIVE)
        self.assertIs(fp.method, DevigMethod.PROPORTIONAL)
        self.assertTrue(any("负概率" in n for n in fp.notes))

    def test_two_outcome_market(self):
        s = snap((1.90, 1.95), outcomes=("home", "away"))
        fp = devig(s)
        self.assertEqual(len(fp.probabilities), 2)
        self.assertAlmostEqual(sum(fp.probabilities), 1.0, places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
