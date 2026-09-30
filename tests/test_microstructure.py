#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.microstructure 的单元测试（报告 §5.1）。"""

from __future__ import annotations

import unittest
from datetime import timedelta

from core.microstructure import (
    SUSPENSION_HEAVY_RATIO,
    cross_book_spread,
    extract_signals,
    is_suspended_heavy,
    trend_direction,
)
from core.models import OddsSnapshot, SnapshotState, utcnow


def series(odds_states, match_id="2001", step=10, outcomes=("home", "draw", "away")):
    """按 (赔率, 状态) 序列构造时间递增的快照。"""
    t0 = utcnow()
    out = []
    for i, (o, st) in enumerate(odds_states):
        if isinstance(o, (int, float)):
            trip = (float(o), 3.45, 2.70)
        else:
            trip = tuple(float(x) for x in o)
        out.append(OddsSnapshot(
            match_id=match_id, league="日职", home="A", away="B", market="1X2",
            outcomes=outcomes, odds=trip, state=st,
            captured_at=t0 + timedelta(seconds=i * step),
        ))
    return out


A = SnapshotState.ACTIVE
S = SnapshotState.SUSPENDED


class TestSuspensionHeavy(unittest.TestCase):
    def test_threshold_behaviour(self):
        self.assertFalse(is_suspended_heavy(0, 100))
        self.assertFalse(is_suspended_heavy(29, 100))
        self.assertTrue(is_suspended_heavy(31, 100))

    def test_zero_window_is_heavy(self):
        self.assertTrue(is_suspended_heavy(0, 0))

    def test_ratio_constant(self):
        self.assertAlmostEqual(SUSPENSION_HEAVY_RATIO, 0.30, places=12)


class TestExtractSignals(unittest.TestCase):
    def test_empty_series(self):
        sig = extract_signals([])
        self.assertEqual(sig.tick_count, 0)
        self.assertFalse(sig.usable)
        self.assertTrue(any("为空" in n for n in sig.notes))

    def test_no_ticks_when_constant_odds(self):
        sig = extract_signals(series([(2.10, A), (2.10, A), (2.10, A)]))
        self.assertEqual(sig.tick_count, 0)
        self.assertAlmostEqual(sig.tick_frequency, 0.0, places=12)
        self.assertTrue(any("未观察到" in n for n in sig.notes))

    def test_counts_ticks(self):
        sig = extract_signals(series([(2.10, A), (2.20, A), (2.30, A), (2.40, A)]))
        self.assertEqual(sig.tick_count, 3)
        self.assertGreater(sig.tick_frequency, 0.0)

    def test_drift_rate_signed(self):
        up = extract_signals(series([(2.0, A), (2.2, A), (2.4, A)]))
        down = extract_signals(series([(2.4, A), (2.2, A), (2.0, A)]))
        self.assertGreater(up.drift_rate, 0.0)
        self.assertLess(down.drift_rate, 0.0)

    def test_recovery_time_computed(self):
        sig = extract_signals(
            series([(2.0, A), (2.5, A), (2.5, A), (2.5, A), (2.5, A)]),
            min_ticks_for_recovery=1)
        self.assertIsNotNone(sig.recovery_seconds)
        assert sig.recovery_seconds is not None
        self.assertGreaterEqual(sig.recovery_seconds, 0.0)

    def test_suspension_counted_and_excluded_from_ticks(self):
        """报告 §5.3：停盘期为冻结值，不得计入跳动。"""
        sig = extract_signals(series([
            (2.0, A), (2.1, A), (9.9, S), (2.1, A)]))
        self.assertEqual(sig.n_suspended, 1)
        self.assertGreater(sig.suspension_seconds, 0.0)
        # 停盘期间的 9.9 不应产生跳动（2.0→2.1 与 2.1→2.1）
        self.assertEqual(sig.tick_count, 1)

    def test_heavy_suspension_marks_unusable(self):
        """停盘占比过高 → 报告 §5.1 判定信号被风控行为主导。"""
        sig = extract_signals(series([
            (2.0, S), (2.0, S), (2.0, S), (2.0, S), (2.0, A)]))
        self.assertFalse(sig.usable)
        self.assertTrue(any("不可用" in n or "主导" in n for n in sig.notes))

    def test_outcome_index_out_of_range(self):
        with self.assertRaises(ValueError):
            extract_signals(series([(2.0, A)]), outcome_index=5)

    def test_mixed_match_ids_rejected(self):
        s1 = series([(2.0, A)])
        s2 = series([(2.0, A)], match_id="9999")
        with self.assertRaises(ValueError):
            extract_signals(s1 + s2)

    def test_outcome_index_selects_correct_series(self):
        sig = extract_signals(series([(2.0, A), (3.0, A)]), outcome_index=1)
        self.assertEqual(sig.outcome, "draw")

    def test_as_dict_json_safe(self):
        import json
        json.dumps(extract_signals(series([(2.0, A), (2.1, A)])).as_dict())

    def test_window_seconds(self):
        sig = extract_signals(series([(2.0, A), (2.1, A), (2.2, A)], step=10))
        self.assertAlmostEqual(sig.window_seconds, 20.0, places=6)


class TestCrossBookSpread(unittest.TestCase):
    def test_single_source_is_zero(self):
        self.assertAlmostEqual(cross_book_spread({"a": (0.5, 0.5)}), 0.0, places=12)

    def test_empty_is_zero(self):
        self.assertAlmostEqual(cross_book_spread({}), 0.0, places=12)

    def test_spread_is_max_minus_min(self):
        s = cross_book_spread({"a": (0.50, 0.5), "b": (0.55, 0.45),
                               "c": (0.52, 0.48)})
        self.assertAlmostEqual(s, 0.05, places=12)

    def test_outcome_index(self):
        s = cross_book_spread({"a": (0.5, 0.30), "b": (0.5, 0.40)},
                              outcome_index=1)
        self.assertAlmostEqual(s, 0.10, places=12)

    def test_out_of_range(self):
        with self.assertRaises(ValueError):
            cross_book_spread({"a": (0.5,)}, outcome_index=3)


class TestTrendDirection(unittest.TestCase):
    def test_up(self):
        sigs = [extract_signals(series([(2.0, A), (2.4, A)]))]
        self.assertEqual(trend_direction(sigs), 1)

    def test_down(self):
        sigs = [extract_signals(series([(2.4, A), (2.0, A)]))]
        self.assertEqual(trend_direction(sigs), -1)

    def test_flat(self):
        sigs = [extract_signals(series([(2.0, A), (2.0, A)]))]
        self.assertEqual(trend_direction(sigs), 0)

    def test_empty(self):
        self.assertEqual(trend_direction([]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
