#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入场门控（`core.entry_gate`）的单元测试。

被验证的设计（互联网调研结论 → 代码）：
  [A] EV 阈值分级：基准 2%（SharpAPI 默认档位）
  [B] 门槛随不确定性放大（长赔 / 去水分歧 / 样本稀疏）——「固定阈值是错的」
  [C] 进球后重定价窗口（刚跳价时价格未稳 → 加价）
  [D] 走势逆风 / 资金流（降赔=资金流入=顺风）

关键设计约束（回归点）：
  * **阶段 1 `gate_market()` 不能用 edge 当门槛**：
    去水后的概率就是市场概率，小模型 edge 恒 ≤ 0（报告 §8.2），
    若在此卡 edge 就永远不放行 → 整条决策链路做死。
  * 仅当**存在走势数据**时，`pre_edge > 0` 才作为阶段 1 的硬条件。
  * 阶段 2 `evaluate_entry()` 才做最终 edge 判定。

    python3 -m unittest tests.test_entry_gate -v
"""

from __future__ import annotations

import unittest

from core.entry_gate import (
    REJECT_EDGE,
    REJECT_ILLIQUID,
    REJECT_METHOD_SPREAD,
    REJECT_STATE,
    REJECT_TREND,
    EntryGateConfig,
    evaluate_entry,
    gate_market,
    required_edge,
)


# --------------------------------------------------------------------------- #
# required_edge：门槛构造
# --------------------------------------------------------------------------- #

class TestRequiredEdge(unittest.TestCase):
    def test_base_threshold_is_two_percent(self) -> None:
        """[A] 走势数据充足时，门槛就是基准 2%。

        注意要传 `trend_ticks`：默认 0 会触发 thin_data 加价
        （无走势数据 = 信息缺失，这是有意为之，见下一个用例）。
        """
        req, parts = required_edge(1.90, trend_ticks=10)
        self.assertAlmostEqual(req, 0.02, places=9)
        self.assertAlmostEqual(parts["base"], 0.02, places=9)

    def test_no_trend_data_adds_penalty(self) -> None:
        """无走势数据 = 信息缺失 → 门槛抬高到 2.5%（有意为之）。

        依据调研 [B]：「你的概率估计才是脆弱输入」，
        缺少微结构信息时应更保守。
        """
        cfg = EntryGateConfig()
        req, parts = required_edge(1.90, trend_ticks=0)
        self.assertAlmostEqual(req, cfg.base_min_edge + cfg.thin_data_penalty,
                               places=9)
        self.assertAlmostEqual(parts["thin_data"], cfg.thin_data_penalty,
                               places=9)

    def test_longshot_adds_penalty(self) -> None:
        """[B] 长赔（≥4.0）需要更高门槛：同样 2% 在高赔上更容易是噪声。"""
        short, _ = required_edge(1.90, trend_ticks=10)
        long_, parts = required_edge(4.50, trend_ticks=10)
        self.assertGreater(long_, short)
        self.assertGreater(parts["longshot"], 0.0)

    def test_method_disagreement_adds_penalty(self) -> None:
        """[B] 去水方法分歧越大 → 公平概率本身越不确定 → 门槛越高。"""
        clean, _ = required_edge(1.90, method_spread_pp=0.0, trend_ticks=10)
        dirty, parts = required_edge(1.90, method_spread_pp=2.0, trend_ticks=10)
        self.assertGreater(dirty, clean)
        self.assertGreater(parts["method_disagreement"], 0.0)

    def test_method_disagreement_is_capped(self) -> None:
        """分歧加价必须有上限，否则极端脏数据会把门槛推到不可能。"""
        _, parts = required_edge(1.90, method_spread_pp=999.0, trend_ticks=10)
        self.assertLessEqual(parts["method_disagreement"],
                             EntryGateConfig().method_spread_cap + 1e-9)

    def test_adverse_trend_adds_penalty(self) -> None:
        """[D] 市场正朝我们反方向定价 → 我们的估计可能已过期。"""
        calm, _ = required_edge(1.90, trend_pct=0.05, trend_against=False,
                                trend_ticks=10)
        adverse, parts = required_edge(1.90, trend_pct=0.05, trend_against=True,
                                       trend_ticks=10)
        self.assertGreater(adverse, calm)
        self.assertGreater(parts["adverse_trend"], 0.0)

    def test_small_trend_does_not_add_penalty(self) -> None:
        """小幅逆风不算「逆风」：低于阈值不应加价（避免噪声放大）。"""
        cfg = EntryGateConfig()
        _, parts = required_edge(1.90, trend_pct=0.001, trend_against=True,
                                 trend_ticks=10)
        self.assertEqual(parts["adverse_trend"], 0.0)
        self.assertLess(0.001, cfg.adverse_trend_threshold)

    def test_repricing_window_adds_penalty(self) -> None:
        """[C] 刚发生过跳动（如进球）→ 价格尚未稳定 → 门槛抬高。"""
        stale, _ = required_edge(1.90, tick_age_s=600.0, trend_ticks=10)
        fresh, parts = required_edge(1.90, tick_age_s=5.0, trend_ticks=10)
        self.assertGreater(fresh, stale)
        self.assertGreater(parts["repricing_window"], 0.0)

    def test_thin_data_adds_penalty(self) -> None:
        """[B] 走势样本不足 ≈ 没有走势信息 → 加价。"""
        rich, _ = required_edge(1.90, trend_ticks=20)
        thin, parts = required_edge(1.90, trend_ticks=1)
        self.assertGreater(thin, rich)
        self.assertGreater(parts["thin_data"], 0.0)

    def test_penalties_accumulate(self) -> None:
        """多项不利因素应叠加（保守方向）。"""
        one, _ = required_edge(4.50, trend_ticks=10)
        both, _ = required_edge(4.50, method_spread_pp=2.0, tick_age_s=1.0,
                                trend_ticks=10)
        self.assertGreater(both, one)

    def test_garbage_input_does_not_raise(self) -> None:
        """上游 JSON 可能给出字符串/None：必须容错而不是抛异常。"""
        for bad in (None, "abc", float("nan"), float("inf")):
            with self.subTest(bad=bad):
                req, _ = required_edge(bad)  # type: ignore[arg-type]
                self.assertGreaterEqual(req, 0.0)


# --------------------------------------------------------------------------- #
# 阶段 1：gate_market —— 不能用 edge 当门槛（核心回归）
# --------------------------------------------------------------------------- #

class TestGateMarketStage1(unittest.TestCase):
    def test_does_not_use_edge_as_gate(self) -> None:
        """**核心回归**：无走势数据时，edge 恒 ≤ 0 也必须能放行。

        去水后的概率就是市场概率，小模型 edge 必然 ≤ 0（报告 §8.2）。
        若阶段 1 拿 edge 当门槛，就永远不放行 → 决策链路被做死。
        """
        r = gate_market(odds=1.90, state="active", trend_ticks=0)
        self.assertTrue(r.passed, r.rejects)
        self.assertIsNone(r.edge, "阶段 1 不应做 edge 判定")

    def test_passes_healthy_market(self) -> None:
        r = gate_market(odds=1.90, state="active", method_spread_pp=0.5,
                        trend="flat", trend_ticks=10)
        self.assertTrue(r.passed)

    def test_rejects_suspended_state(self) -> None:
        r = gate_market(odds=1.90, state="suspended")
        self.assertFalse(r.passed)
        self.assertIn(REJECT_STATE, r.rejects)

    def test_rejects_delisted_state(self) -> None:
        r = gate_market(odds=1.90, state="delisted")
        self.assertFalse(r.passed)
        self.assertIn(REJECT_STATE, r.rejects)

    def test_rejects_unreliable_devig(self) -> None:
        """去水方法分歧过大 → 市场公平概率本身不可信，问 LLM 也是错的。"""
        r = gate_market(odds=1.90, method_spread_pp=5.0)
        self.assertFalse(r.passed)
        self.assertIn(REJECT_METHOD_SPREAD, r.rejects)

    def test_rejects_large_adverse_trend(self) -> None:
        """走势明确且大幅逆风 → 方向已不利，不是「不确定」。"""
        r = gate_market(odds=1.90, trend="up", trend_pct=0.15, trend_ticks=10)
        self.assertFalse(r.passed)
        self.assertIn(REJECT_TREND, r.rejects)

    def test_small_adverse_trend_still_passes(self) -> None:
        r = gate_market(odds=1.90, trend="up", trend_pct=0.01, trend_ticks=10)
        self.assertNotIn(REJECT_TREND, r.rejects)

    def test_liquidity_floor_when_configured(self) -> None:
        cfg = EntryGateConfig(min_bet_amount=1000.0)
        low = gate_market(odds=1.90, bet_amount=10.0, config=cfg)
        ok = gate_market(odds=1.90, bet_amount=5000.0, config=cfg)
        self.assertIn(REJECT_ILLIQUID, low.rejects)
        self.assertTrue(ok.passed)

    def test_liquidity_not_checked_by_default(self) -> None:
        """默认不设流动性下限（上游 betAmount 语义未充分验证）。"""
        r = gate_market(odds=1.90, bet_amount=0.0)
        self.assertNotIn(REJECT_ILLIQUID, r.rejects)

    # -- 走势信息优势（仅在有走势数据时）----------------------------------- #

    def test_pre_edge_gate_only_applies_with_trend_data(self) -> None:
        """有走势 + pre_edge ≤ 0 → 拦下（市场在往反方向走）。"""
        r = gate_market(odds=1.90, trend_ticks=10, pre_edge=-0.01)
        self.assertFalse(r.passed)
        self.assertIn(REJECT_EDGE, r.rejects)

    def test_positive_pre_edge_passes(self) -> None:
        """有走势 + pre_edge > 0 → 放行（信息流入但价格未走完）。"""
        r = gate_market(odds=1.90, trend_ticks=10, pre_edge=0.03)
        self.assertTrue(r.passed, r.rejects)

    def test_no_trend_means_pre_edge_ignored(self) -> None:
        """无走势数据时 pre_edge 不参与（否则永远不放行）。"""
        r = gate_market(odds=1.90, trend_ticks=0, pre_edge=-0.05)
        self.assertTrue(r.passed, r.rejects)

    def test_can_disable_pre_edge_requirement(self) -> None:
        cfg = EntryGateConfig(require_pre_edge_when_trended=False)
        r = gate_market(odds=1.90, trend_ticks=10, pre_edge=-0.05, config=cfg)
        self.assertTrue(r.passed)

    def test_reports_threshold_for_stage2(self) -> None:
        """阶段 1 虽不判 edge，但仍须算出门槛供阶段 2 使用。"""
        r = gate_market(odds=1.90)
        self.assertGreater(r.required_edge, 0.0)
        self.assertIn("threshold_parts", r.checks)


# --------------------------------------------------------------------------- #
# 阶段 2：evaluate_entry —— 最终 edge 判定
# --------------------------------------------------------------------------- #

class TestEvaluateEntryStage2(unittest.TestCase):
    def test_passes_when_edge_clears_threshold(self) -> None:
        r = evaluate_entry(outcome="home", odds=1.90, edge=0.05)
        self.assertTrue(r.passed, r.rejects)
        self.assertGreater(r.margin, 0.0)

    def test_rejects_below_threshold(self) -> None:
        r = evaluate_entry(outcome="home", odds=1.90, edge=0.005)
        self.assertFalse(r.passed)
        self.assertIn(REJECT_EDGE, r.rejects)
        self.assertLess(r.margin, 0.0)

    def test_same_edge_rejected_at_long_odds(self) -> None:
        """**调研 [B] 的落地**：同样 2.5% edge，短赔过、长赔被拒。

        短赔门槛 2%，长赔门槛 3%（2% + 1% 长赔加价）。
        这正是「固定阈值是错的」的可执行体现。
        """
        short = evaluate_entry(outcome="home", odds=1.90, edge=0.025,
                               trend_ticks=10)
        long_ = evaluate_entry(outcome="home", odds=6.00, edge=0.025,
                               trend_ticks=10)
        self.assertTrue(short.passed, short.rejects)
        self.assertFalse(long_.passed)
        self.assertGreater(long_.required_edge, short.required_edge)

    def test_longshot_penalty_is_flat_not_scaling(self) -> None:
        """诚实标注简化：长赔加价是**固定 1%**，不随赔率继续放大。

        调研 [B] 只给出方向（长赔要求更高），未给标定曲线；
        本项目取固定加价作为保守近似。此用例锁定该行为，
        避免日后误以为是按赔率缩放。
        """
        cfg = EntryGateConfig()
        _, p1 = required_edge(cfg.longshot_odds, trend_ticks=10)
        _, p2 = required_edge(50.0, trend_ticks=10)
        self.assertEqual(p1["longshot"], p2["longshot"])
        self.assertAlmostEqual(p1["longshot"], cfg.longshot_penalty, places=9)

    def test_repricing_window_raises_bar(self) -> None:
        """刚跳价（如进球后）→ 同样 edge 可能不再够。"""
        calm = evaluate_entry(outcome="home", odds=1.90, edge=0.025,
                              tick_age_s=600.0, trend_ticks=10)
        fresh = evaluate_entry(outcome="home", odds=1.90, edge=0.025,
                               tick_age_s=2.0, trend_ticks=10)
        self.assertTrue(calm.passed, calm.rejects)
        self.assertFalse(fresh.passed)

    def test_hard_rejects_unreliable_devig(self) -> None:
        r = evaluate_entry(outcome="home", odds=1.90, edge=0.50,
                           method_spread_pp=9.0)
        self.assertFalse(r.passed)
        self.assertIn(REJECT_METHOD_SPREAD, r.rejects)

    def test_hard_rejects_large_adverse_trend_despite_big_edge(self) -> None:
        """大幅逆风是硬拒绝：加价无法补偿「方向已明确不利」。"""
        r = evaluate_entry(outcome="home", odds=1.90, edge=0.50,
                           trend="up", trend_pct=0.20)
        self.assertFalse(r.passed)
        self.assertIn(REJECT_TREND, r.rejects)

    def test_rejects_untradeable_state(self) -> None:
        r = evaluate_entry(outcome="home", odds=1.90, edge=0.50,
                           state="suspended")
        self.assertFalse(r.passed)
        self.assertIn(REJECT_STATE, r.rejects)

    def test_checks_are_auditable(self) -> None:
        """每个拒绝都必须能逐项复核「为什么是这个门槛」。"""
        r = evaluate_entry(outcome="home", odds=4.50, edge=0.02,
                           method_spread_pp=1.0, tick_age_s=3.0,
                           trend_ticks=1)
        d = r.as_dict()
        self.assertIn("threshold_parts", d["checks"])
        parts = d["checks"]["threshold_parts"]
        for key in ("base", "longshot", "method_disagreement",
                    "repricing_window", "thin_data", "total"):
            self.assertIn(key, parts)
        self.assertAlmostEqual(parts["total"], d["required_edge"], places=6)

    def test_garbage_input_does_not_raise(self) -> None:
        r = evaluate_entry(outcome="home", odds="1.9", edge=None,  # type: ignore[arg-type]
                           trend_pct="x", method_spread_pp=None)  # type: ignore[arg-type]
        self.assertIsInstance(r.passed, bool)


# --------------------------------------------------------------------------- #
# 两阶段一致性
# --------------------------------------------------------------------------- #

class TestStageConsistency(unittest.TestCase):
    def test_stage2_threshold_matches_stage1(self) -> None:
        """两阶段算出的门槛必须一致，否则前端展示会自相矛盾。"""
        kw = dict(odds=3.20, method_spread_pp=1.5, trend="up",
                  trend_pct=0.03, tick_age_s=10.0, trend_ticks=2)
        s1 = gate_market(**kw)              # type: ignore[arg-type]
        s2 = evaluate_entry(outcome="home", edge=0.0, **kw)  # type: ignore[arg-type]
        self.assertAlmostEqual(s1.required_edge, s2.required_edge, places=9)

    def test_stage1_pass_does_not_imply_stage2_pass(self) -> None:
        """阶段 1 通过 ≠ 可买入：最终仍要 LLM 的 edge 过门槛。"""
        s1 = gate_market(odds=1.90, trend_ticks=10, pre_edge=0.01)
        self.assertTrue(s1.passed)
        s2 = evaluate_entry(outcome="home", odds=1.90, edge=0.005,
                            trend_ticks=10)
        self.assertFalse(s2.passed)

    def test_same_edge_short_vs_long_uses_trend_ticks(self) -> None:
        """长赔拒绝与走势样本充足情形一致（不受 thin_data 干扰）。"""
        short = evaluate_entry(outcome="home", odds=1.90, edge=0.025,
                               trend_ticks=10)
        long_ = evaluate_entry(outcome="home", odds=6.00, edge=0.025,
                               trend_ticks=10)
        self.assertTrue(short.passed, short.rejects)
        self.assertFalse(long_.passed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
