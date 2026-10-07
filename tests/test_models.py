#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.models 的单元测试（AGENTS.md §4.3：正常路径 + 边界/异常路径）。"""

from __future__ import annotations

import unittest
from datetime import timedelta
from typing import Any, cast

from core.models import (
    DevigMethod,
    ExecutionFilter,
    FairProbabilities,
    OddsSnapshot,
    SizingResult,
    SnapshotState,
    utcnow,
)


def make_snap(**kw):
    base = dict(
        match_id="2001", league="日职", home="鹿岛鹿角", away="大阪樱花",
        market="1X2", outcomes=("home", "draw", "away"), odds=(2.13, 3.45, 2.70),
    )
    base.update(kw)
    # 本工厂故意接受任意覆盖项以简化用例（含刻意非法的值），
    # 用 cast(Any, ...) 显式声明“有意放宽类型”。
    return OddsSnapshot(**cast(Any, base))


class TestOddsSnapshot(unittest.TestCase):
    """快照构造、派生量与校验。"""

    def test_valid_snapshot_derived_values(self):
        s = make_snap()
        self.assertAlmostEqual(s.booksum, sum(1 / o for o in s.odds), places=12)
        self.assertAlmostEqual(s.margin, s.booksum - 1.0, places=12)
        self.assertEqual(s.n_outcomes, 3)
        self.assertEqual(len(s.raw_implied), 3)
        self.assertAlmostEqual(sum(s.raw_implied), s.booksum, places=12)

    def test_default_state_is_active_and_usable(self):
        s = make_snap()
        self.assertIs(s.state, SnapshotState.ACTIVE)
        self.assertTrue(s.is_usable())

    def test_as_dict_is_json_safe(self):
        import json
        d = make_snap().as_dict()
        json.dumps(d)  # 不应抛异常
        self.assertEqual(d["state"], "active")
        self.assertIn("margin", d)
        self.assertIn("booksum", d)

    # -- 边界与异常 ---------------------------------------------------------

    def test_empty_match_id_rejected(self):
        with self.assertRaises(ValueError):
            make_snap(match_id="")

    def test_length_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            make_snap(outcomes=("home", "draw"), odds=(2.0, 3.0, 4.0))

    def test_single_outcome_rejected(self):
        with self.assertRaises(ValueError):
            make_snap(outcomes=("home",), odds=(2.0,))

    def test_duplicate_outcomes_rejected(self):
        with self.assertRaises(ValueError):
            make_snap(outcomes=("home", "home", "away"), odds=(2.0, 3.0, 4.0))

    def test_odds_le_one_rejected(self):
        with self.assertRaises(ValueError):
            make_snap(odds=(1.0, 3.45, 2.70))

    def test_odds_non_finite_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                make_snap(odds=(bad, 3.45, 2.70))

    def test_odds_non_numeric_rejected(self):
        with self.assertRaises((TypeError, ValueError)):
            make_snap(odds=("abc", 3.45, 2.70))

    def test_odds_bool_rejected(self):
        with self.assertRaises((TypeError, ValueError)):
            make_snap(odds=(True, 3.45, 2.70))

    def test_decimal_string_odds_accepted(self):
        """上游 JSON 用字符串存赔率，应能正常转换。"""
        s = make_snap(odds=("2.13", "3.45", "2.70"))
        self.assertAlmostEqual(s.odds[0], 2.13, places=10)

    def test_negative_margin_when_arbitrage(self):
        """booksum < 1（真实套利或解析错误）应产生负水钱，而非报错。"""
        s = make_snap(odds=(3.0, 3.0, 3.0))  # booksum = 1.0
        self.assertAlmostEqual(s.margin, 0.0, places=12)
        s2 = make_snap(odds=(4.0, 4.0, 4.0))  # booksum = 0.75 < 1
        self.assertLess(s2.margin, 0.0)


class TestSnapshotState(unittest.TestCase):
    """状态语义（报告 §4.2 / §5.3）。"""

    def test_only_active_usable_for_signal(self):
        self.assertTrue(SnapshotState.ACTIVE.usable_for_signal)
        for st in (SnapshotState.SUSPENDED, SnapshotState.STALE,
                   SnapshotState.DELISTED):
            self.assertFalse(st.usable_for_signal)

    def test_quote_usable_except_delisted(self):
        self.assertTrue(SnapshotState.SUSPENDED.usable_for_quote)
        self.assertTrue(SnapshotState.STALE.usable_for_quote)
        self.assertFalse(SnapshotState.DELISTED.usable_for_quote)

    def test_suspended_snapshot_not_usable(self):
        self.assertFalse(make_snap(state=SnapshotState.SUSPENDED).is_usable())

    def test_state_from_value(self):
        self.assertIs(SnapshotState("suspended"), SnapshotState.SUSPENDED)
        with self.assertRaises(ValueError):
            SnapshotState("nonexistent")


class TestFairProbabilities(unittest.TestCase):
    """去水结果模型的校验。"""

    def test_valid_and_sums_to_one(self):
        fp = FairProbabilities(probabilities=(0.4, 0.3, 0.3),
                               method=DevigMethod.SHIN, margin=0.05,
                               outcomes=("home", "draw", "away"))
        self.assertTrue(fp.sums_to_one)
        self.assertIn("probabilities", fp.as_dict())

    def test_probability_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            FairProbabilities(probabilities=(1.4, 0.0, 0.0),
                              method=DevigMethod.SHIN, margin=0.0,
                              outcomes=("a", "b", "c"))
        with self.assertRaises(ValueError):
            FairProbabilities(probabilities=(-0.1, 0.6, 0.5),
                              method=DevigMethod.SHIN, margin=0.0,
                              outcomes=("a", "b", "c"))

    def test_nan_probability_rejected(self):
        with self.assertRaises(ValueError):
            FairProbabilities(probabilities=(float("nan"), 0.5, 0.5),
                              method=DevigMethod.SHIN, margin=0.0,
                              outcomes=("a", "b", "c"))


class TestExecutionFilter(unittest.TestCase):
    """执行过滤（报告 §8.3）。"""

    def test_valid(self):
        f = ExecutionFilter(q_fill=0.9, stake=100.0, cost_exec=0.001)
        self.assertAlmostEqual(f.effective_multiplier, 0.9)

    def test_q_fill_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            ExecutionFilter(q_fill=1.5, stake=100.0)

    def test_negative_stake_rejected(self):
        with self.assertRaises(ValueError):
            ExecutionFilter(q_fill=0.9, stake=-1.0)


class TestSizingResult(unittest.TestCase):
    """仓位结果模型。"""

    def test_as_dict(self):
        r = SizingResult(fractions=(0.01, 0.0, 0.0), outcomes=("a", "b", "c"),
                         lam=0.25, used_covariance=False,
                         correlation_penalty=1.0, total_exposure=0.01)
        d = r.as_dict()
        self.assertEqual(d["lam"], 0.25)
        self.assertAlmostEqual(d["total_exposure"], 0.01)


class TestUtcnow(unittest.TestCase):
    """时间工具必须带时区（避免 naive/aware 比较错误）。"""

    def test_is_timezone_aware(self):
        t = utcnow()
        self.assertIsNotNone(t.tzinfo)
        self.assertIsNotNone(t.utcoffset())

    def test_comparable_with_aware_datetime(self):
        t = utcnow()
        other = t + timedelta(seconds=1)
        self.assertLess(t, other)

    def test_timezone_is_utc(self):
        self.assertEqual(utcnow().utcoffset(), timedelta(0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
