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
import unittest
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional
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


if __name__ == "__main__":
    unittest.main(verbosity=2)


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
