#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘口汇总式决策的单元测试（T9）。

校验用户要求的架构：
    经济算法先算完一场的全部盘口 → 汇总 → **每场只调 1 次 LLM** → 买入裁定

关键回归点：
  1. 小模型的 edge 必然 ≤ 0（去水概率即市场概率，报告 §8.2）
     —— 因此若只让 LLM「从已有 edge 里挑」，就永远挑不出东西。
     正确做法是让 LLM 给**独立概率**，由它产生 edge。
  2. LLM 给出偏离市场的概率时，必须能产出买入建议（机制可用）。
  3. LLM 照抄市场时，不得产出买入建议（诚实性）。
  4. LLM 幻觉盘口/结果必须被丢弃。
  5. 每场 LLM 调用次数 == 1（不是每盘口一次）。

不发起真实网络请求。

    python3 -m unittest tests.test_match_decision -v
"""

from __future__ import annotations

import json
import time
import unittest
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, cast
from unittest import mock

from core.models import OddsSnapshot, SnapshotState
from service.decision import (
    DECISION_AVOID,
    DECISION_BUY,
    DECISION_NO_LLM,
    DecisionConfig,
)
from service.llm import LLMClient, LLMConfig, LLMError
from service.match_decision import (
    MAX_MARKETS_PER_PROMPT,
    MatchDecisionEngine,
    MatchPicks,
    _as_float,
    _as_int,
)

_KEY = "sk-test-not-real"
_BASE = "http://llm.test:8885/v1"


def _snap(market: str, outcomes=("home", "away"), odds=(1.90, 2.00),
          mid: str = "m1", state=SnapshotState.ACTIVE) -> OddsSnapshot:
    return OddsSnapshot(
        match_id=mid, league="测试联赛", home="主队", away="客队",
        market=market, outcomes=outcomes, odds=odds, state=state,
        captured_at=datetime.now(timezone.utc), source="乐鱼API",
    )


def _three_markets(mid: str = "m1") -> List[OddsSnapshot]:
    return [
        _snap("AH(0)", ("home", "away"), (1.90, 2.00), mid),
        _snap("OU(2.5)", ("over", "under"), (1.85, 2.05), mid),
        _snap("HAD", ("home", "draw", "away"), (2.10, 3.40, 3.60), mid),
    ]


def _engine(llm_reply: Any = None, **cfg: Any) -> MatchDecisionEngine:
    e = MatchDecisionEngine(DecisionConfig(**cfg))
    if llm_reply is not None:
        c = LLMClient(LLMConfig(base_url=_BASE, model="m", api_key=_KEY))
        mock.patch.object(c, "complete_json", return_value=llm_reply).start()
        unittest.TestCase().addCleanup(mock.patch.stopall)
        e.attach_llm(c)
    return e


class TestSmallModelEdge(unittest.TestCase):
    def test_market_probabilities_yield_non_positive_edge(self) -> None:
        """核心事实：小模型 edge 必然 ≤ 0。

        这不是缺陷，而是报告 §8.2 的直接结论：
        去水后的公平概率就是市场概率，没有独立信息就没有优势。
        """
        e = _engine()
        comps = e.compute_markets(_three_markets())
        self.assertTrue(comps)
        for c in comps:
            self.assertLessEqual(c.best_edge, 1e-9,
                                 "小模型不应凭空产生正优势")
            self.assertTrue(all(e_ <= 1e-9 for e_ in c.edges))

    def test_compute_markets_makes_no_llm_call(self) -> None:
        """经济学层必须是纯计算（不发 LLM）——否则就退回逐盘口调用的老问题。"""
        e = _engine()
        comps = e.compute_markets(_three_markets())
        self.assertEqual(len(comps), 3)
        self.assertEqual(e.stats["llm_calls"], 0)

    def test_suspended_markets_are_skipped(self) -> None:
        e = _engine()
        snaps = _three_markets()
        snaps.append(_snap("AH(1)", state=SnapshotState.SUSPENDED))
        comps = e.compute_markets(snaps)
        self.assertNotIn("AH(1)", [c.market for c in comps])

    def test_candidates_sorted_by_best_edge(self) -> None:
        e = _engine()
        comps = e.compute_markets(_three_markets())
        edges = [c.best_edge for c in comps]
        self.assertEqual(edges, sorted(edges, reverse=True))


class TestSingleLlmCallPerMatch(unittest.TestCase):
    def test_one_call_for_whole_match(self) -> None:
        """每场只调 1 次 LLM（旧实现是每盘口一次 → 一场 14 次 → 504）。"""
        e = _engine({"markets": []})
        e.decide_match(_three_markets())
        self.assertEqual(e.stats["llm_calls"], 1)
        assert e.llm is not None
        self.assertEqual(e.llm.calls if hasattr(e.llm, "calls") else 0, 0)
        # 验证 complete_json 只被调用一次
        self.assertEqual(e.llm.complete_json.call_count, 1)  # type: ignore[attr-defined]

    def test_no_llm_degrades_honestly(self) -> None:
        e = _engine()
        r = e.decide_match(_three_markets())
        self.assertEqual(r.decision, DECISION_NO_LLM)
        self.assertFalse(r.picks)

    def test_llm_failure_degrades(self) -> None:
        e = _engine()
        c = LLMClient(LLMConfig(base_url=_BASE, model="m", api_key=_KEY))
        with mock.patch.object(c, "complete_json", side_effect=LLMError("上游 502")):
            e.attach_llm(c)
            r = e.decide_match(_three_markets())
        self.assertEqual(r.decision, DECISION_NO_LLM)
        self.assertIn("502", r.llm_reason)

    def test_llm_timeout_degrades(self) -> None:
        """超时必须降级，不能拖住整批（实测单场曾达 295s）。"""
        e = _engine(llm_timeout_s=0.01)
        c = LLMClient(LLMConfig(base_url=_BASE, model="m", api_key=_KEY))

        def slow(*_a: Any, **_k: Any) -> Any:
            import time as _t
            _t.sleep(0.4)
            return {"markets": []}

        with mock.patch.object(c, "complete_json", side_effect=slow):
            e.attach_llm(c)
            r = e.decide_match(_three_markets())
        self.assertEqual(r.decision, DECISION_NO_LLM)
        self.assertIn("超时", r.llm_reason)


class TestBuyMechanism(unittest.TestCase):
    """证明机制可用：LLM 偏离市场时能产出买入建议。"""

    def test_deviation_produces_buy(self) -> None:
        # AH(0) 赔率 1.90/2.00，市场概率约 0.50/0.50（去水后）
        # LLM 若认为 home 有 0.60，则 edge = 0.60*1.90-1 = +0.14 → 应买入
        reply = {"markets": [
            {"market": "AH(0)", "probabilities": {"home": 0.60, "away": 0.40},
             "confidence": 0.8, "reason": "主队状态好"},
        ]}
        e = _engine(reply)
        r = e.decide_match(_three_markets())
        self.assertEqual(r.decision, DECISION_BUY)
        self.assertTrue(r.picks)
        top = r.best_pick
        assert top is not None
        self.assertEqual(top["market"], "AH(0)")
        self.assertEqual(top["outcome"], "home")
        self.assertGreater(top["edge"], 0)
        self.assertGreater(top["kelly"], 0)
        self.assertLessEqual(top["kelly"], e.config.max_stake_pct)
        self.assertEqual(top["reason"], "主队状态好")

    def test_echoing_market_produces_no_buy(self) -> None:
        """诚实性：LLM 照抄市场 → 无优势 → 不给买入建议。"""
        reply = {"markets": [
            {"market": "AH(0)", "probabilities": {"home": 0.5, "away": 0.5},
             "confidence": 0.9, "reason": "无独立看法"},
        ]}
        e = _engine(reply)
        r = e.decide_match(_three_markets())
        self.assertEqual(r.decision, DECISION_AVOID)
        self.assertEqual(r.picks, [])

    def test_low_confidence_rejected(self) -> None:
        reply = {"markets": [
            {"market": "AH(0)", "probabilities": {"home": 0.60, "away": 0.40},
             "confidence": 0.3, "reason": "不确定"},
        ]}
        e = _engine(reply, min_confidence=0.5)
        r = e.decide_match(_three_markets())
        self.assertEqual(r.picks, [])

    def test_hallucinated_market_is_dropped(self) -> None:
        """LLM 编造不存在的盘口必须丢弃（防幻觉）。"""
        reply = {"markets": [
            {"market": "不存在的盘口", "probabilities": {"home": 0.99, "away": 0.01},
             "confidence": 0.9, "reason": "幻觉"},
        ]}
        e = _engine(reply)
        r = e.decide_match(_three_markets())
        self.assertEqual(r.picks, [])

    def test_incomplete_probabilities_rejected(self) -> None:
        """概率未覆盖全部结果 → 无法算优势 → 丢弃。"""
        reply = {"markets": [
            {"market": "HAD", "probabilities": {"home": 0.9, "draw": 0.1},
             "confidence": 0.9, "reason": "缺 away"},
        ]}
        e = _engine(reply)
        r = e.decide_match(_three_markets())
        self.assertEqual(r.picks, [])

    def test_probabilities_normalised(self) -> None:
        """和为 2 的非法输入应归一化，且结果可复核。"""
        reply = {"markets": [
            {"market": "AH(0)", "probabilities": {"home": 1.2, "away": 0.8},
             "confidence": 0.8, "reason": "和不为1"},
        ]}
        e = _engine(reply)
        r = e.decide_match(_three_markets())
        for p in r.picks:
            self.assertAlmostEqual(p["p_llm"], 0.6, places=6)

    def test_market_probability_is_reported(self) -> None:
        """必须回传市场概率，便于人工复核三套概率。"""
        reply = {"markets": [
            {"market": "AH(0)", "probabilities": {"home": 0.60, "away": 0.40},
             "confidence": 0.8, "reason": ""},
        ]}
        e = _engine(reply)
        r = e.decide_match(_three_markets())
        top = r.best_pick
        assert top is not None
        self.assertIn("p_market", top)
        self.assertIn("p_llm", top)


class TestEntryGateWiring(unittest.TestCase):
    """门控与决策链路的接线（用户要求的工作逻辑）。

    工作逻辑：**经济学算法满足后才调用 LLM**。
    因此必须验证：
      1. 全场不通过时**不调 LLM**（省下调用与延迟）；
      2. 通过时只把**通过的盘口**交给 LLM；
      3. 阶段 2 用盘口**自己的动态门槛**判定 LLM 的 edge。
    """

    def test_all_rejected_skips_llm_entirely(self) -> None:
        """阶段 1 全部拦下 → LLM 零调用（省下调用与延迟）。

        构造：盘口本身可交易，但**去水方法分歧过大**
        （`devig_unreliable`）——这是阶段 1 的硬拒绝，
        与「停盘/下架」不同（后者在 `compute_markets` 就被滤掉）。
        """
        snaps = [_snap("AH(0)", ("home", "away"), (1.90, 2.00))]
        calls = {"n": 0}

        def _boom(*a: Any, **kw: Any) -> Any:
            calls["n"] += 1
            return {"markets": []}

        e = _engine()
        # 让去水分歧超过阶段 1 上限（默认 3.0 个百分点）
        with mock.patch.object(
                e.small, "_small_model",
                return_value=((0.5, 0.5), {"margin": 0.05,
                                           "method": "proportional",
                                           "method_spread_pp": 9.0}, [])):
            c = LLMClient(LLMConfig(base_url=_BASE, model="m", api_key=_KEY))
            with mock.patch.object(c, "complete_json", side_effect=_boom):
                e.attach_llm(c)
                r = e.decide_match(snaps)
        self.assertEqual(calls["n"], 0, "未通过门控不应调用 LLM")
        self.assertEqual(r.decision, DECISION_AVOID)
        self.assertFalse(r.llm_used)
        self.assertIn("入场门槛", r.llm_reason)
        self.assertEqual(e.stats["gated_out_matches"], 1)

    def test_suspended_snapshots_are_filtered_before_gate(self) -> None:
        """停盘/下架在 `compute_markets` 就被滤掉（报告 §5.3），
        不会进入门控；此时给出的是「无可用盘口」而不是门控理由。
        """
        snaps = [
            _snap("AH(0)", ("home", "away"), (1.90, 2.00),
                  state=SnapshotState.SUSPENDED),
            _snap("OU(2.5)", ("over", "under"), (1.85, 2.05),
                  state=SnapshotState.DELISTED),
        ]
        e = _engine({"markets": []})
        r = e.decide_match(snaps)
        self.assertEqual(r.decision, DECISION_AVOID)
        self.assertIn("无可用盘口", r.error)
        self.assertEqual(r.gated_in, 0)
        self.assertEqual(r.gated_out, 0)

    def test_only_passing_markets_reach_llm(self) -> None:
        """LLM 上下文只含通过门控的盘口（被阶段 1 拒的不进 prompt）。"""
        snaps = _three_markets() + [
            _snap("AH(1)", ("home", "away"), (1.90, 2.00)),
        ]
        seen: Dict[str, Any] = {}

        def _cap(prompt: str, **kw: Any) -> Any:
            seen["prompt"] = prompt
            return {"markets": []}

        e = _engine()
        # 只让 AH(1) 的去水分歧过大 → 它被阶段 1 拦下，
        # 其余三个盘口正常通过（验证“只把通过的交给 LLM”）。
        real = e.small._small_model
        bad = ((0.5, 0.5), {"margin": 0.05, "method": "proportional",
                            "method_spread_pp": 9.0}, [])

        def _small(snap: Any, trend: Any) -> Any:
            if snap.market == "AH(1)":
                return bad
            return real(snap, trend)

        c = LLMClient(LLMConfig(base_url=_BASE, model="m", api_key=_KEY))
        with mock.patch.object(e.small, "_small_model", side_effect=_small):
            with mock.patch.object(c, "complete_json", side_effect=_cap):
                e.attach_llm(c)
                r = e.decide_match(snaps)
        self.assertIn("prompt", seen)
        self.assertNotIn("AH(1)", seen["prompt"], "被门控拦下的盘口不应进 prompt")
        self.assertGreater(r.gated_in, 0)
        self.assertGreaterEqual(r.gated_out, 1)

    def test_gate_stats_are_reported(self) -> None:
        """门控统计必须可观测（供 /health 与排障）。"""
        e = _engine({"markets": []})
        r = e.decide_match(_three_markets())
        d = r.as_dict()
        for key in ("gated_in", "gated_out", "reject_reasons"):
            self.assertIn(key, d)
        self.assertEqual(d["gated_in"] + d["gated_out"], d["n_computed"])

    def test_health_exposes_entry_gate_config(self) -> None:
        e = _engine()
        h = e.health()
        self.assertIn("entry_gate", h)
        self.assertIn("base_min_edge", h["entry_gate"])
        self.assertAlmostEqual(h["entry_gate"]["base_min_edge"], 0.02, places=9)

    def test_dynamic_threshold_used_for_llm_edge(self) -> None:
        """阶段 2 必须用盘口自己的门槛，而不是全局 min_edge。

        构造：长赔（门槛 3%）+ 2.5% edge → 应被拒；
        若错误地用全局 2%，就会被误判为可买入。
        """
        snaps = [_snap("AH(0)", ("home", "away"), (6.00, 1.15))]
        reply = {"markets": [
            {"market": "AH(0)", "probabilities": {"home": 0.17, "away": 0.83},
             "confidence": 0.9, "reason": ""},
        ]}
        e = _engine(reply)
        comps = e.compute_markets(snaps)
        self.assertTrue(comps, "应有可用盘口")
        self.assertGreater(comps[0].gates[0].required_edge, 0.02,
                           "长赔门槛应高于基准")
        r = e.decide_match(snaps)
        self.assertEqual(r.picks, [], "2.5% edge 不应过长赔门槛")

    def test_pick_reports_required_edge(self) -> None:
        """买入建议必须回传门槛，便于人工复核。"""
        reply = {"markets": [
            {"market": "AH(0)", "probabilities": {"home": 0.65, "away": 0.35},
             "confidence": 0.9, "reason": ""},
        ]}
        e = _engine(reply)
        r = e.decide_match(_three_markets())
        top = r.best_pick
        self.assertIsNotNone(top)
        assert top is not None
        self.assertIn("required_edge", top)
        self.assertIn("required_edge_pct", top)


class TestTrendIndexing(unittest.TestCase):
    """走势索引键的回归（本项目真实缺陷）。

    走势来自 WS 推送，键为上游 `(chpid, hv)`；
    而 `snap.market` 是归一化代码（`AH(0.5)`）。
    早期实现用 `snap.market` 去查，两者**永不可能相等**，
    导致走势维度一直为空（形同死代码）。
    """

    def test_trend_matched_by_chpid_and_hv(self) -> None:
        snap = _snap("AH(0.5)", ("home", "away"), (1.90, 2.00))
        object.__setattr__(snap, "metadata", {"leyu_chpid": "4",
                                               "leyu_hv": "0.5"})
        trend = {"markets": [{
            "chpid": "4", "hv": "0.5", "n": 7,
            "last": {"direction": "down", "delta_pct": -2.5, "ts": 0},
        }]}
        e = _engine()
        comps = e.compute_markets([snap], trend=trend)
        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0].trend_n, 7, "走势必须能匹配上")
        self.assertEqual(comps[0].trend, "down")

    def test_trend_not_matched_by_market_code(self) -> None:
        """反证：`market` 代码作键时匹配不上（旧实现的做法）。"""
        snap = _snap("AH(0.5)", ("home", "away"), (1.90, 2.00))
        object.__setattr__(snap, "metadata", {"leyu_chpid": "4",
                                               "leyu_hv": "0.5"})
        # 用 market 代码当 chpid → 不应匹配
        trend = {"markets": [{
            "chpid": "AH(0.5)", "hv": "", "n": 7,
            "last": {"direction": "down", "delta_pct": -2.5, "ts": 0},
        }]}
        e = _engine()
        comps = e.compute_markets([snap], trend=trend)
        self.assertEqual(comps[0].trend_n, 0)

    def test_chpid_fallback_when_hv_differs(self) -> None:
        """同一 chpid 下不同线值必须能区分，但缺失 hv 时可回退。"""
        snap = _snap("OU(2.5)", ("over", "under"), (1.85, 2.05))
        object.__setattr__(snap, "metadata", {"leyu_chpid": "2",
                                               "leyu_hv": "2.5"})
        trend = {"markets": [{
            "chpid": "2", "hv": "", "n": 4,
            "last": {"direction": "up", "delta_pct": 1.0, "ts": 0},
        }]}
        e = _engine()
        comps = e.compute_markets([snap], trend=trend)
        self.assertEqual(comps[0].trend_n, 4)


class TestParallelAndRanking(unittest.TestCase):
    def test_decide_many_parallel(self) -> None:
        e = _engine({"markets": []}, llm_timeout_s=30.0)
        items = [
            ("m1", "L", "A", "B", _three_markets("m1"), None, None),
            ("m2", "L", "C", "D", _three_markets("m2"), None, None),
        ]
        out = e.decide_many(items, max_workers=2)
        self.assertEqual(len(out), 2)
        self.assertEqual(e.stats["matches"], 2)

    def test_rank_prefers_buys(self) -> None:
        buy = MatchPicks(match_id="a")
        buy.picks = [{"edge": 0.1, "confidence": 0.8}]
        no = MatchPicks(match_id="b")
        self.assertGreater(buy.rank_score, no.rank_score)
        self.assertTrue(buy.has_buy)
        self.assertFalse(no.has_buy)

    def test_as_dict_json_serialisable(self) -> None:
        e = _engine()
        r = e.decide_match(_three_markets())
        json.dumps(r.as_dict())

    def test_health_reports_stats(self) -> None:
        e = _engine()
        h = e.health()
        self.assertIn("stats", h)
        self.assertIn("llm", h)


class TestPromptAndHelpers(unittest.TestCase):
    def test_prompt_includes_market_probabilities(self) -> None:
        e = _engine()
        comps = e.compute_markets(_three_markets())
        p = e.build_prompt(comps, "主队", "客队", "联赛", {"score": "1:0"})
        self.assertIn("主队", p)
        self.assertIn("1:0", p)
        self.assertIn("市场公平概率", p)
        self.assertLessEqual(len(comps), MAX_MARKETS_PER_PROMPT + 3)

    def test_candidate_cap_respected(self) -> None:
        e = _engine()
        snaps = [_snap("AH(%d)" % i, ("home", "away"), (1.9, 2.0))
                 for i in range(30)]
        comps = e.compute_markets(snaps)
        p = e.build_prompt(comps, "A", "B", "L", None)
        # 提示词里不应出现超过上限的盘口
        appears = sum(1 for c in comps[:MAX_MARKETS_PER_PROMPT]
                      if c.market in p)
        self.assertLessEqual(appears, MAX_MARKETS_PER_PROMPT)

    def test_tolerant_converters(self) -> None:
        self.assertEqual(_as_float("1.5"), 1.5)
        self.assertEqual(_as_float(None, 2.0), 2.0)
        self.assertEqual(_as_float("x", 3.0), 3.0)
        self.assertEqual(_as_float(float("nan"), 4.0), 4.0)
        self.assertEqual(_as_int("7"), 7)
        self.assertEqual(_as_int(None, 1), 1)

    def test_failed_match_does_not_break_batch(self) -> None:
        e = _engine({"markets": []})
        with mock.patch.object(e, "decide_match",
                               side_effect=RuntimeError("boom")):
            out = e.decide_many([
                ("m1", "L", "A", "B", _three_markets("m1"), None, None)])
        self.assertEqual(len(out), 1)



class TestScheduledCycle(unittest.TestCase):
    """后台定时决策（用户要求：不依赖页面刷新）。"""

    def _svc(self, **kw: Any) -> Any:
        from service.analysis import AnalysisConfig, AnalysisService
        cfg = AnalysisConfig(use_llm=False, **kw)
        return AnalysisService(valuation=mock.MagicMock(), realtime=None,
                               config=cfg)

    def test_latest_result_none_before_first_round(self) -> None:
        svc = self._svc(cycle_interval_s=0)
        self.assertIsNone(svc.latest_result())

    def test_cycle_disabled_when_interval_zero(self) -> None:
        svc = self._svc(cycle_interval_s=0)
        self.assertFalse(svc.start_cycle())
        self.assertFalse(svc.cycle_running)

    def test_run_cycle_once_caches_result(self) -> None:
        svc = self._svc()
        fake = {"count": 2, "summary": {"buy": 1}, "decisions": []}
        with mock.patch.object(svc, "decide_list", return_value=fake):
            svc._run_cycle_once()
        got = svc.latest_result()
        self.assertIsNotNone(got)
        assert got is not None
        self.assertTrue(got.get("cycle"))
        self.assertEqual(svc.cycle_stats["rounds"], 1)

    def test_cycle_survives_exception(self) -> None:
        """单轮失败不能让定时循环退出（否则系统静默停止更新）。"""
        svc = self._svc()
        with mock.patch.object(svc, "decide_list", side_effect=RuntimeError("boom")):
            svc._run_cycle_once()
        self.assertIn("boom", svc.cycle_stats["last_error"])
        self.assertEqual(svc.cycle_stats["rounds"], 0)

    def test_max_age_filters_stale(self) -> None:
        svc = self._svc()
        with mock.patch.object(svc, "decide_list",
                               return_value={"count": 0, "decisions": []}):
            svc._run_cycle_once()
        self.assertIsNotNone(svc.latest_result(max_age_s=3600))
        self.assertIsNone(svc.latest_result(max_age_s=0.000001))

    def test_persist_and_restore(self) -> None:
        """落盘后重启能立即读到上次结果（不必等首轮）。"""
        import os as _os
        import tempfile
        path = _os.path.join(tempfile.mkdtemp(), "decisions.json")
        svc = self._svc(result_path=path)
        with mock.patch.object(svc, "decide_list",
                               return_value={"count": 1, "decisions": [{"a": 1}]}):
            svc._run_cycle_once()
        self.assertTrue(_os.path.exists(path))
        # 新实例应能恢复
        svc2 = self._svc(result_path=path)
        got = svc2.latest_result()
        self.assertIsNotNone(got)
        assert got is not None
        self.assertTrue(got.get("restored"))

    def test_corrupt_result_file_ignored(self) -> None:
        import os as _os
        import tempfile
        path = _os.path.join(tempfile.mkdtemp(), "decisions.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        svc = self._svc(result_path=path)
        self.assertIsNone(svc.latest_result())  # 损坏则忽略，不抛异常


class TestNoArtificialCap(unittest.TestCase):
    """用户要求：展示应与乐鱼接口的进行中数量一致，不应人为截断。

    历史缺陷：三处硬编码 60（全量采集 / WS 订阅 / 决策上限），
    导致「进行中 50 场」时看着够用，一旦赛程密集就会静默丢赛事。
    """

    def test_cycle_limit_zero_means_unlimited(self) -> None:
        from service.analysis import DEFAULT_CYCLE_LIMIT, AnalysisConfig
        self.assertEqual(DEFAULT_CYCLE_LIMIT, 0)
        self.assertEqual(AnalysisConfig().cycle_limit, 0)

    def test_partial_max_is_not_the_live_cap(self) -> None:
        """DEFAULT_PARTIAL_MAX 只用于「非全量拉取」的安全阀，不是进行中上限。"""
        from collector.sources import DEFAULT_PARTIAL_MAX
        self.assertGreater(DEFAULT_PARTIAL_MAX, 0)

    def test_candidates_limit_zero_returns_all(self) -> None:
        """关键回归：0 必须表示「不限」，而不是返回空列表。"""
        from service.analysis import AnalysisConfig, AnalysisService

        svc = AnalysisService(
            valuation=mock.MagicMock(), realtime=None,
            config=AnalysisConfig(use_llm=False, cycle_limit=0))
        svc.valuation.list_matches.return_value = [
            {"match_id": str(i), "markets": ["HAD"], "state": "active"}
            for i in range(25)
        ]
        svc.live_match_ids = lambda: None  # 退回按 state 判定
        got = svc.candidates(limit=0)
        self.assertEqual(len(got), 25, "limit=0 应返回全部，而不是空")

    def test_candidates_positive_limit_truncates(self) -> None:
        from service.analysis import AnalysisConfig, AnalysisService

        svc = AnalysisService(
            valuation=mock.MagicMock(), realtime=None,
            config=AnalysisConfig(use_llm=False))
        svc.valuation.list_matches.return_value = [
            {"match_id": str(i), "markets": ["HAD"], "state": "active"}
            for i in range(25)
        ]
        svc.live_match_ids = lambda: None
        self.assertEqual(len(svc.candidates(limit=5)), 5)

    def test_refresh_default_is_unlimited(self) -> None:
        import inspect

        from service.analysis import AnalysisService
        sig = inspect.signature(AnalysisService.refresh_live_matches)
        self.assertEqual(sig.parameters["max_matches"].default, 0)

    def test_realtime_hub_does_not_cap_by_default(self) -> None:
        """订阅默认**不截断**：用户要求与乐鱼进行中数量一致。"""
        from collector.leyu_realtime import RealtimeHub

        h = RealtimeHub(session_provider=None,
                        mids_provider=lambda: [str(i) for i in range(120)])
        self.assertEqual(h.max_matches, 0)
        self.assertEqual(len(h._pick_mids()), 120)

    def test_realtime_hub_cap_when_explicit(self) -> None:
        from collector.leyu_realtime import RealtimeHub

        h = RealtimeHub(session_provider=None,
                        mids_provider=lambda: [str(i) for i in range(120)],
                        max_matches=10)
        self.assertEqual(len(h._pick_mids()), 10)

    def test_subscription_source_filters_soccer(self) -> None:
        """订阅源必须只取**进行中的足球**：乐鱼同网关也返回篮球/网球。

        过滤逻辑由 `SnapshotSource.live_match_ids()` 提供，
        `api.app._start_background` 的 `_mids()` 直接委托它
        （早期在 API 层内联写过滤，导致此处只能靠字符串匹配源码来断言；
        现已下沉到基类，可直接做**行为**断言）。
        """
        from collector.sources import SOCCER_SPORT_ID, SnapshotSource

        src = SnapshotSource.live_match_ids.__doc__ or ""
        self.assertIn("sport_id", src)

        # 行为断言：构造一个假的源，验证过滤 + 排序无关性
        class _Fake(SnapshotSource):  # type: ignore[misc]
            name = "fake"

            def fetch(self, *a: Any, **kw: Any) -> Any:
                return [], []

            def schedule(self) -> List[Any]:
                class _M:
                    def __init__(self, mid: str, sport: str, live: bool) -> None:
                        self.mid, self.sport_id, self.is_live = mid, sport, live

                return [
                    _M("soccer-live", SOCCER_SPORT_ID, True),
                    _M("basket-live", "2", True),      # 篮球：必须被排除
                    _M("soccer-soon", SOCCER_SPORT_ID, False),  # 未开赛：排除
                    _M("tennis-live", "5", True),      # 网球：排除
                ]

        fake = _Fake()
        self.assertEqual(fake.live_match_ids(), ["soccer-live"])
        # 传空串可关闭运动筛选（拿全部进行中）
        self.assertEqual(sorted(fake.live_match_ids("")),
                         ["basket-live", "soccer-live", "tennis-live"])
        counts = fake.live_count()
        self.assertEqual(counts["live"], 1)
        self.assertEqual(counts["live_all_sports"], 3)
        self.assertEqual(counts["scheduled"], 2)

    def test_api_delegates_live_filtering_to_source(self) -> None:
        """API 层应**委托**而不是重复实现过滤（避免两处逻辑漂移）。"""
        import inspect

        import api.app as app_mod
        src = inspect.getsource(app_mod._start_background)
        self.assertIn("live_match_ids", src)
        self.assertIn("SOCCER_SPORT_ID", src)


class TestPriceChangeTrigger(unittest.TestCase):
    """盘口变动触发决策（用户要求：盘口变化时自动触发）。

    原实现只按固定 600s 定时轮询 —— 要么错过时机，要么在行情不动时
    空烧 LLM。本组用例锁定新契约：

      1. Hub 只在**真实**变动时回调（首次观测/无变化不回调）
      2. 回调必须极快返回（不能同步跑 LLM）
      3. 同一场在防抖窗口内被合并为一次
      4. 全局最小间隔防止行情暴涨时打满 LLM
      5. 触发式决策必须先**刷新快照**，否则算的是旧赔率
      6. 触发结果要**合并**进 latest，不能把其它场次抹掉
    """

    def _svc(self, **kw: Any) -> Any:
        from service.analysis import AnalysisConfig, AnalysisService
        cfg = AnalysisConfig(use_llm=False, **kw)
        return AnalysisService(valuation=mock.MagicMock(), realtime=None,
                               config=cfg)

    # -- Hub 侧 -----------------------------------------------------------

    def test_hub_fires_callback_only_on_real_change(self) -> None:
        """首次观测建立基线不算变动；赔率不变不回调。"""
        from collector.leyu_realtime import PriceTick, RealtimeHub

        seen: List[List[str]] = []
        h = RealtimeHub(session_provider=None, on_price_change=seen.append)

        t = PriceTick(mid="m1", chpid="c1", hid="h", hv="0",
                      oid="o1", ot="1", old_ov=0.0, new_ov=2.0, ts_ms=1)
        h._record_ticks([t])                      # 首次：只建基线
        self.assertEqual(seen, [])
        h._record_ticks([t])                      # 同价：无变动
        self.assertEqual(seen, [])

        t2 = PriceTick(mid="m1", chpid="c1", hid="h", hv="0",
                       oid="o1", ot="1", old_ov=0.0, new_ov=1.8, ts_ms=2)
        h._record_ticks([t2])                     # 真变动
        self.assertEqual(seen, [["m1"]])

    def test_hub_dedupes_mids_in_one_batch(self) -> None:
        """同一批里多盘口变动 → 只回调一次，mid 去重。"""
        from collector.leyu_realtime import PriceTick, RealtimeHub

        seen: List[List[str]] = []
        h = RealtimeHub(session_provider=None, on_price_change=seen.append)
        base = [PriceTick(mid="m1", chpid="c1", hid="h", hv="0",
                          oid="o%d" % i, ot="1", old_ov=0.0, new_ov=2.0,
                          ts_ms=1) for i in range(3)]
        h._record_ticks(base)
        h._record_ticks([PriceTick(mid=t.mid, chpid=t.chpid, hid=t.hid,
                                   hv=t.hv, oid=t.oid, ot=t.ot, old_ov=0.0,
                                   new_ov=1.5, ts_ms=2) for t in base])
        self.assertEqual(seen, [["m1"]])

    def test_hub_callback_exception_does_not_kill_push(self) -> None:
        """回调抛异常不得影响推送链路（否则连行情都看不到）。"""
        from collector.leyu_realtime import PriceTick, RealtimeHub

        def boom(_mids: List[str]) -> None:
            raise RuntimeError("callback bug")

        h = RealtimeHub(session_provider=None, on_price_change=boom)
        t0 = PriceTick(mid="m1", chpid="c1", hid="h", hv="0", oid="o",
                       ot="1", old_ov=0.0, new_ov=2.0, ts_ms=1)
        h._record_ticks([t0])
        h._record_ticks([PriceTick(mid="m1", chpid="c1", hid="h", hv="0",
                                   oid="o", ot="1", old_ov=0.0, new_ov=1.9,
                                   ts_ms=2)])
        self.assertEqual(h.stats.price_ticks, 1)
        self.assertIn("callback bug", h.stats.last_error)

    # -- 调度器侧 ---------------------------------------------------------

    def test_notify_is_fast_and_does_not_run_llm(self) -> None:
        """回调必须极快返回：它在推送消费线程上跑。"""
        svc = self._svc(change_debounce_s=30.0)
        with mock.patch.object(svc, "decide_matches") as dm:
            svc.notify_price_change(["m1", "m2"])
            dm.assert_not_called()          # 不在调用方线程里决策
        self.assertEqual(len(svc._pending), 2)

    def test_debounce_coalesces_repeat_signals(self) -> None:
        """同一场重复变动合并为一次，且计时往后顺延。"""
        svc = self._svc(change_debounce_s=30.0)
        svc.notify_price_change(["m1"])
        first = svc._pending["m1"]
        svc.notify_price_change(["m1"])
        self.assertEqual(len(svc._pending), 1)
        self.assertGreaterEqual(svc._pending["m1"], first)
        self.assertEqual(svc._sched_stats["coalesced"], 1)

    def test_disabled_trigger_ignores_signals(self) -> None:
        svc = self._svc(change_trigger=False)
        svc.notify_price_change(["m1"])
        self.assertEqual(svc._pending, {})
        self.assertFalse(svc.start_scheduler())

    def test_queue_max_drops_oldest(self) -> None:
        """队列满时丢弃最久未变动的赛事（最可能已无价值）。"""
        svc = self._svc(change_queue_max=3, change_debounce_s=0.0)
        for i in range(6):
            svc.notify_price_change(["m%d" % i])
            svc._pending["m%d" % i] = float(i)   # 人为拉开先后
        self.assertLessEqual(len(svc._pending), 3)
        self.assertGreater(svc._sched_stats["dropped"], 0)
        self.assertNotIn("m0", svc._pending)     # 最早的被丢

    def test_next_trigger_wait_none_when_idle(self) -> None:
        svc = self._svc()
        self.assertIsNone(svc._next_trigger_wait())

    def test_global_min_interval_throttles(self) -> None:
        """刚触发过 → 下一批必须等够全局最小间隔。"""
        svc = self._svc(change_min_interval_s=20.0, change_debounce_s=0.0)
        svc.notify_price_change(["m1"])
        svc._sched_stats["last_trigger_at"] = time.time()
        wait = svc._next_trigger_wait()
        assert wait is not None
        self.assertGreater(wait, 15.0)

    def test_trigger_batch_calls_decide_matches(self) -> None:
        svc = self._svc(change_debounce_s=0.0, change_batch=2)
        fake = {"count": 1, "summary": {}, "decisions": []}
        with mock.patch.object(svc, "decide_matches",
                               return_value=fake) as dm:
            svc.notify_price_change(["m1", "m2", "m3"])
            svc._trigger_batch()
            dm.assert_called_once()
            picked = dm.call_args[0][0]
            self.assertEqual(len(picked), 2)     # batch 上限生效
        # 剩余的仍留在队列里（不丢，只是排队）
        self.assertEqual(len(svc._pending), 1)

    # -- decide_matches 行为 -------------------------------------------------

    def test_decide_matches_refreshes_snapshots_first(self) -> None:
        """关键回归：必须先刷新快照，否则用的是旧赔率。"""
        svc = self._svc()
        snap = _mk_snap("m1")
        svc.candidates = lambda **kw: [{"match_id": "m1", "league": "L",
                                        "home": "A", "away": "B"}]
        svc.valuation._snapshots_of = lambda mid: [snap]
        order: List[str] = []
        svc.refresh_matches = lambda mids: order.append("refresh") or {}
        with mock.patch.object(svc.match_engine, "decide_match",
                               return_value=_mp("m1")):
            svc.decide_matches(["m1"])
        self.assertEqual(order, ["refresh"])

    def test_decide_matches_merges_and_keeps_other_matches(self) -> None:
        """触发式结果必须**合并**，不能把其它场次抹掉。"""
        svc = self._svc()
        svc._latest = {
            "count": 1, "summary": {}, "decisions": [
                {"match_id": "keep", "rank_score": 0.5, "has_buy": False}],
        }
        snap = _mk_snap("m1")
        svc.candidates = lambda **kw: [{"match_id": "m1", "league": "L",
                                        "home": "A", "away": "B"}]
        svc.valuation._snapshots_of = lambda mid: [snap]
        svc.refresh_matches = lambda mids: {}
        with mock.patch.object(svc.match_engine, "decide_match",
                               return_value=_mp("m1")):
            res = svc.decide_matches(["m1"])
        ids = {d["match_id"] for d in res["decisions"]}
        self.assertEqual(ids, {"keep", "m1"})
        self.assertEqual(res["trigger"], "price_change")
        self.assertEqual(res["triggered"], ["m1"])

    def test_decide_matches_empty_mids_is_noop(self) -> None:
        svc = self._svc()
        svc._latest = {"count": 0, "summary": {}, "decisions": []}
        self.assertIs(svc.decide_matches([]), svc._latest)

    def test_scheduler_health_exposes_state(self) -> None:
        svc = self._svc()
        h = svc.scheduler_health()
        self.assertIn("running", h)
        self.assertIn("pending", h)
        self.assertIn("stats", h)

    def test_cycle_backs_off_when_trigger_enabled(self) -> None:
        """触发式为主时，定时循环应降频（避免双重浪费）。"""
        import inspect

        from service.analysis import AnalysisService
        src = inspect.getsource(AnalysisService._cycle_loop)
        self.assertIn("change_trigger", src)


def _mk_snap(mid: str) -> Any:
    return OddsSnapshot(
        match_id=mid, league="L", home="A", away="B",
        market="HAD", outcomes=("H", "D", "A"),
        odds=(2.0, 3.3, 3.6), state=SnapshotState.ACTIVE,
        source="leyu", captured_at=datetime.now(timezone.utc),
    )


def _mp(mid: str) -> Any:
    from service.match_decision import MatchPicks
    return MatchPicks(match_id=mid, home="A", away="B")

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestScoreLeakRegression(unittest.TestCase):
    """回归：**绝不把比分/比赛时钟下发给 LLM**（真实故障）。

    实测故障：早期 `AnalysisService._build_context` 把
    `realtime.score(mid)` 放进上下文，`build_prompt` 又把它写进提示词，
    于是 LLM 直接“抄答案”：

        reason = "终场0:0，小球" / "主队2:0取胜，平手盘主胜"
        p_llm = 1.0  而  p_market = 0.35

    这既制造假买入信号，又让赛后统计的命中率虚高（把答案泄给考生）。
    """

    def _svc(self, **kw: Any) -> Any:
        from service.analysis import AnalysisConfig, AnalysisService

        class _Hub:
            def score(self, mid: str) -> Any:
                return (3, 0)

            def status(self, mid: str) -> Any:
                return {"mst": "87", "mmp": "2"}

            def is_finished(self, mid: str) -> bool:
                return False

            def score_age_s(self, mid: str) -> Any:
                return 1.0

            def first_seen_age_s(self, mid: str) -> Any:
                return 1.0

        cfg = AnalysisConfig(use_llm=False, **kw)
        # 传替身对象：本组用例只验证「上下文/提示词里有没有比分」，
        # 不依赖 RealtimeHub 的其余能力，故用 cast 明确意图。
        return AnalysisService(valuation=mock.MagicMock(),
                               realtime=cast(Any, _Hub()),
                               config=cfg)

    def test_context_omits_score_by_default(self) -> None:
        svc = self._svc()
        ctx = svc._build_context({"match_id": "m1", "state": "active"})
        self.assertNotIn("score", ctx)
        self.assertNotIn("minute", ctx)

    def test_prompt_contains_no_score_text(self) -> None:
        """端到端：即使拿到了比分，提示词里也不能出现它。"""
        svc = self._svc()
        ctx = svc._build_context({"match_id": "m1", "state": "active"})
        e = _engine(None)
        comps = e.compute_markets(_three_markets())
        prompt = e.build_prompt(comps, "主队", "客队", "联赛", ctx)
        self.assertNotIn("3:0", prompt)
        self.assertNotIn("当前比分", prompt)
        self.assertNotIn("第 87 分钟", prompt)

    def test_leak_switch_is_explicit_and_off_by_default(self) -> None:
        from service.analysis import AnalysisConfig
        self.assertFalse(AnalysisConfig().leak_score_to_llm)

    def test_leak_switch_on_restores_old_behaviour_for_ab_test(self) -> None:
        """开关打开时恢复旧行为 —— 用于复现“泄露导致假 edge”的对照实验。"""
        svc = self._svc(leak_score_to_llm=True)
        ctx = svc._build_context({"match_id": "m1", "state": "active"})
        self.assertEqual(ctx.get("score"), "3:0")


class TestContaminatedProbabilityGuard(unittest.TestCase):
    """回归：LLM 概率若与市场偏离过大，判为泄露/幻觉并丢弃。"""

    def test_extreme_deviation_is_dropped(self) -> None:
        # 市场约 0.50/0.50（AH(0) 1.90/2.00 去水后），LLM 给 1.0/0.0
        reply = {"markets": [
            {"market": "AH(0)", "probabilities": {"home": 1.0, "away": 0.0},
             "confidence": 0.95, "reason": "终场0:0"},
        ]}
        e = _engine(reply)
        r = e.decide_match(_three_markets())
        # 偏离 0.5 > 0.35 阈值 → 丢弃，不给任何买入建议
        self.assertEqual(r.picks, [])
        self.assertGreaterEqual(e.stats.get("contaminated", 0), 1)

    def test_moderate_deviation_still_allowed(self) -> None:
        # 偏离 0.10（0.60 vs 0.50）→ 正常通过护栏
        reply = {"markets": [
            {"market": "AH(0)", "probabilities": {"home": 0.60, "away": 0.40},
             "confidence": 0.8, "reason": "主队状态好"},
        ]}
        e = _engine(reply)
        r = e.decide_match(_three_markets())
        self.assertEqual(r.decision, DECISION_BUY)
        self.assertEqual(e.stats.get("contaminated", 0), 0)


class TestLlmBudgetAndTimeout(unittest.TestCase):
    """回归：超时与 prompt 规模可调，避免大批场次因超时降级为 no_llm。

    实测：默认 90s 时一轮 71 场中 **11 场** 因超时降级
    （全部报“LLM 决策超时（>90s）”）；单场均值 28.85s，
    推理型模型尾部很长。
    """

    def test_default_timeout_is_raised_from_90(self) -> None:
        self.assertGreaterEqual(DecisionConfig().llm_timeout_s, 150.0)

    def test_timeout_is_configurable(self) -> None:
        e = _engine(None, llm_timeout_s=200.0)
        self.assertEqual(e.config.llm_timeout_s, 200.0)

    def test_prompt_market_cap_is_configurable(self) -> None:
        e = _engine(None, max_markets_per_prompt=2)
        comps = e.compute_markets(_three_markets())
        prompt = e.build_prompt(comps, "主", "客", "联赛", None)
        # 只应出现 2 个盘口标题行（[代码] 形式）
        heads = sum(1 for line in prompt.splitlines()
                    if line.startswith("盘口 ["))
        self.assertLessEqual(heads, 2)

    def test_timeout_message_is_actionable(self) -> None:
        """超时降级必须给出可操作提示（而不是静默无建议）。"""
        import time as _t

        e = _engine(None, llm_timeout_s=0.01)
        c = LLMClient(LLMConfig(base_url=_BASE, model="m", api_key=_KEY))

        def _slow(*a: Any, **k: Any) -> Any:
            _t.sleep(0.5)
            return {"markets": []}

        c.complete_json = _slow          # type: ignore[assignment]
        e.attach_llm(c)
        r = e.decide_match(_three_markets())
        self.assertEqual(r.decision, DECISION_NO_LLM)
        self.assertIn("超时", r.llm_reason)
