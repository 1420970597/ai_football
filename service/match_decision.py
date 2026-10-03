#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘口汇总式决策（T9）—— 经济算法算全部盘口 → 汇总 → 每场一次 LLM 决策。

## 为什么不逐盘口问 LLM

早期实现对**每个盘口**单独调一次 LLM。实测一场平均有 **14.5 个盘口**，
于是单场就要十几次 LLM 调用，6 场列表耗时 300s+，直接 HTTP 504。

用户要求的正确架构是：

    1. 经济学算法（多个）对**该场全部盘口**各自快速计算
       （去水 5 法 + 水钱 + 方法分歧 + 走势修正 + EV + 分数凯利）
    2. 把全部盘口的结果**汇总成一个上下文**交给 LLM
    3. LLM 只做一次判断：这些盘口里哪个值得买入，以及为什么

于是 LLM 调用从 ~15 次/场 降到 **1 次/场**，6 场并发下约 30~60 秒。

## 输出语义

LLM 返回**对候选盘口的买入裁定**，而不是给每个结果编一套概率：

```json
{
  "picks": [
    {"market": "AH(0.25)", "outcome": "home",
     "confidence": 0.72, "reason": "主队近况好且盘口降赔"},
    ...
  ],
  "note": "整体判断（可选）"
}
```

`picks` 可以为空数组——**允许 LLM 说"这都不值得买"**。
这是本项目一贯的诚实性要求：没有足够证据就不给买入建议。

## 与逐盘口模式的关系

`DecisionEngine.decide()`（逐盘口）保留，用于：
- 单盘口深度分析（详情页）
- 小模型对照
- 无 LLM 时的降级

本模块的 `MatchDecisionEngine` 是列表/批量场景的**主路径**。
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core import devig as devig_mod
from core import economics as econ
from core.models import OddsSnapshot, SnapshotState

from .decision import (
    DECISION_AVOID,
    DECISION_BUY,
    DECISION_NO_LLM,
    DECISION_WATCH,
    DecisionConfig,
    DecisionEngine,
)
from .llm import LLMClient, LLMError, LLMNotConfigured

__all__ = [
    "MarketComputation",
    "MatchPicks",
    "MatchDecisionEngine",
    "MATCH_SYSTEM_PROMPT",
]

#: 交给 LLM 的候选盘口上限（过多会稀释注意力并撑爆上下文）
#: 实测教训：盘口越多、提示词越长，推理型模型的思考时间就越不可控
#: （曾出现单场 331s、推理 18494 字），直接拖垮定时循环。
#: 只把**经济算法认为最有希望的**前 N 个盘口交给 LLM 判断即可。
MAX_CANDIDATES_FOR_LLM = 6

#: 单个盘口最多展示的结果数
MAX_OUTCOMES_PER_MARKET = 3

#: LLM 返回的买入建议上限（防止一次给出一堆"推荐"）
MAX_PICKS = 4

#: 默认并行度（受推理服务承载能力限制）
DEFAULT_MAX_WORKERS = 4

#: 单场 LLM 决策超时（秒）。
#: 实测推理型模型在较大输入上思考时间不可预测（曾出现 295s），
#: 而控制台需要可预期响应。超时即降级为“无买入建议”并说明原因，
#: 绝不让一个慢请求拖住整批决策。
DEFAULT_MATCH_LLM_TIMEOUT_S = 90.0


def _as_float(value: object, default: float = 0.0) -> float:
    """容错浮点转换。

    这些值最终来自 LLM 返回的 dict（经我们校验后重建），
    但上游模型输出不可信，直接 `float()` 会抛 TypeError 并中断整批决策。
    """
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _as_int(value: object, default: int = 0) -> int:
    """容错整数转换（同上）。"""
    try:
        return int(str(value))
    except (TypeError, ValueError, OverflowError):
        return default

MATCH_SYSTEM_PROMPT = """你是足球盘口分析师。系统已用经济学算法算好盘口（去水后市场概率、水钱、盘口走势）。

任务：给出你对盘口结果的**概率估计**。

规则：
1. 只输出严格 JSON，无解释、无 markdown 围栏。
2. 不引用你记忆中的具体赛果。
3. 简短思考后直接给结论，不要长篇推理。
4. probabilities 必须含该盘口**全部结果**，总和为 1。
5. confidence 是你对**自己估计**的把握；仅有赔率而无其他信息时给低值（<0.5）。

输出：
{"markets":[{"market":"<代码>","probabilities":{"<结果>":<0-1>},"confidence":<0-1>,"reason":"<20字内>"}]}"""


@dataclass
class MarketComputation:
    """单个盘口的经济学算法计算结果（LLM 的输入单元）。"""

    market: str
    outcomes: Tuple[str, ...]
    odds: Tuple[float, ...]
    #: 去水后的公平概率（已含走势修正）
    p_fair: Tuple[float, ...]
    #: 各结果的优势 edge = p*odds-1
    edges: Tuple[float, ...]
    #: 各结果的建议仓位（分数凯利）
    kellys: Tuple[float, ...]
    margin: float = 0.0
    method: str = ""
    method_spread_pp: float = 0.0
    state: str = SnapshotState.ACTIVE.value
    #: 走势摘要（该盘口）
    trend: str = "flat"
    trend_pct: float = 0.0
    trend_n: int = 0

    @property
    def best_edge(self) -> float:
        return max(self.edges) if self.edges else -9.0

    @property
    def best_index(self) -> int:
        if not self.edges:
            return -1
        return max(range(len(self.edges)), key=lambda i: self.edges[i])

    def as_dict(self) -> Dict[str, Any]:
        return {
            "market": self.market,
            "outcomes": list(self.outcomes),
            "odds": [round(o, 4) for o in self.odds],
            "p_fair": [round(p, 6) for p in self.p_fair],
            "edges": [round(e, 6) for e in self.edges],
            "kellys": [round(k, 6) for k in self.kellys],
            "margin": round(self.margin, 6),
            "method": self.method,
            "method_spread_pp": round(self.method_spread_pp, 3),
            "state": self.state,
            "trend": self.trend,
            "trend_pct": round(self.trend_pct, 3),
            "trend_n": self.trend_n,
            "best_edge": round(self.best_edge, 6),
            "best_outcome": (self.outcomes[self.best_index]
                             if self.best_index >= 0 else ""),
        }


@dataclass
class MatchPicks:
    """一场比赛的最终决策（含全部盘口计算 + LLM 买入建议）。"""

    match_id: str
    league: str = ""
    home: str = ""
    away: str = ""
    decision: str = DECISION_AVOID
    computations: List[MarketComputation] = field(default_factory=list)
    #: LLM 确认可买入的项
    picks: List[Dict[str, Any]] = field(default_factory=list)
    llm_note: str = ""
    llm_reason: str = ""
    llm_used: bool = False
    llm_confidence: float = 0.0
    n_markets: int = 0
    elapsed_ms: int = 0
    error: str = ""
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def best_pick(self) -> Optional[Dict[str, Any]]:
        if not self.picks:
            return None
        return max(self.picks, key=lambda p: p.get("edge", -9.0))

    @property
    def rank_score(self) -> float:
        """列表排序：优先有买入建议的、edge 高的。"""
        b = self.best_pick
        if b is None:
            return -1.0
        return _as_float(b.get("edge")) * _as_float(b.get("confidence"))

    @property
    def has_buy(self) -> bool:
        return bool(self.picks)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "match_id": self.match_id,
            "league": self.league,
            "home": self.home,
            "away": self.away,
            "decision": self.decision,
            "n_markets": self.n_markets,
            "n_computed": len(self.computations),
            "has_buy": self.has_buy,
            "picks": self.picks,
            "best_pick": self.best_pick,
            "llm_note": self.llm_note,
            "llm_reason": self.llm_reason,
            "llm_used": self.llm_used,
            "llm_confidence": round(self.llm_confidence, 4),
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "computed_at": self.computed_at.isoformat(),
            "rank_score": round(self.rank_score, 6),
            "computations": [c.as_dict() for c in self.computations],
        }


class MatchDecisionEngine:
    """盘口汇总式决策引擎。

    用法::

        eng = MatchDecisionEngine(DecisionConfig())
        eng.attach_llm(LLMClient(load_pi_config()))
        res = eng.decide_match(snapshots, trend=hub.trend(mid), context={...})
    """

    def __init__(self, config: Optional[DecisionConfig] = None,
                 llm: Optional[LLMClient] = None) -> None:
        self.config = config or DecisionConfig()
        #: 复用逐盘口引擎的小模型层（去水与走势修正逻辑只写一处）
        self.small = DecisionEngine(self.config, llm=None)
        self.llm = llm
        self.stats: Dict[str, int] = {
            "matches": 0, "with_picks": 0, "picks": 0,
            "llm_calls": 0, "llm_failures": 0, "no_llm": 0,
        }

    # -- LLM ---------------------------------------------------------------

    def attach_llm(self, llm: Optional[LLMClient]) -> None:
        self.llm = llm

    @property
    def llm_available(self) -> bool:
        return bool(self.config.use_llm and self.llm is not None)

    def llm_health(self) -> Dict[str, Any]:
        if self.llm is None:
            return {"enabled": self.config.use_llm, "available": False,
                    "reason": "未配置 LLM 客户端"}
        return {"enabled": self.config.use_llm, "available": self.llm_available,
                **self.llm.health()}

    # -- 第 1 步：经济学算法算全部盘口 --------------------------------------

    def compute_markets(
        self,
        snapshots: Sequence[OddsSnapshot],
        trend: Optional[Mapping[str, Any]] = None,
    ) -> List[MarketComputation]:
        """对一场的全部盘口做经济学计算（**不发任何 LLM 请求**）。"""
        out: List[MarketComputation] = []
        trend_by_market = self._index_trend(trend)
        # `_small_model` 接受 Dict；这里统一成 dict 以避免只读映射的类型不匹配
        trend_dict: Optional[Dict[str, Any]] = dict(trend) if trend else None
        for snap in snapshots:
            if snap.state in (SnapshotState.SUSPENDED, SnapshotState.DELISTED):
                # 停盘/下架的价格不得参与决策（报告 §5.3）
                continue
            p_small, diag, _ = self.small._small_model(snap, trend_dict)
            odds = tuple(float(x) for x in snap.odds)
            edges = tuple(p * o - 1.0 for p, o in zip(p_small, odds))
            kellys = []
            for p, o, e in zip(p_small, odds, edges):
                k = 0.0
                if e > 0 and o > 1.0:
                    k = econ.fractional_kelly(p, o,
                                              lam=self.config.kelly_fraction)
                    k = max(0.0, min(k, self.config.max_stake_pct))
                kellys.append(k)

            t = trend_by_market.get(snap.market)
            out.append(MarketComputation(
                market=snap.market,
                outcomes=tuple(snap.outcomes),
                odds=odds,
                p_fair=tuple(p_small),
                edges=edges,
                kellys=tuple(kellys),
                margin=_as_float(diag.get("margin"), snap.margin),
                method=str(diag.get("method", "")),
                method_spread_pp=_as_float(diag.get("method_spread_pp")),
                state=snap.state.value,
                trend=(t or {}).get("direction", "flat"),
                trend_pct=_as_float((t or {}).get("delta_pct")),
                trend_n=_as_int((t or {}).get("n")),
            ))
        # 优势大的排前面：LLM 上下文有限，优先给它看有希望的
        out.sort(key=lambda c: c.best_edge, reverse=True)
        return out

    @staticmethod
    def _index_trend(trend: Optional[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """把 Hub 的走势结果索引成 `盘口代码 → 走势摘要`。"""
        if not trend:
            return {}
        markets = trend.get("markets") or []
        idx: Dict[str, Dict[str, Any]] = {}
        for m in markets:
            if not isinstance(m, Mapping):
                continue
            chpid = str(m.get("chpid") or "")
            hv = str(m.get("hv") or "")
            last = m.get("last") or {}
            if not isinstance(last, Mapping):
                continue
            key_variants = [hv, chpid]
            info = {
                "direction": last.get("direction", "flat"),
                "delta_pct": last.get("delta_pct", 0.0),
                "n": m.get("n", 0),
                "up": m.get("up", 0),
                "down": m.get("down", 0),
            }
            for k in key_variants:
                if k:
                    idx.setdefault(k, info)
        return idx

    # -- 第 2 步：汇总成 LLM 上下文 ----------------------------------------

    def build_prompt(
        self,
        comps: Sequence[MarketComputation],
        home: str,
        away: str,
        league: str,
        context: Optional[Mapping[str, Any]] = None,
    ) -> str:
        lines = [
            "这是一场比赛的全部盘口经济学计算结果（市场公平概率已去水）。",
            "请给出**你自己的独立概率估计**，系统会用它重算优势。",
            "",
            "赛事：%s vs %s（%s）" % (home, away, league),
        ]
        ctx = dict(context or {})
        if ctx.get("minute"):
            lines.append("比赛阶段：第 %s 分钟" % ctx["minute"])
        if ctx.get("score"):
            lines.append("当前比分：%s" % ctx["score"])
        if ctx.get("status_text"):
            lines.append("比赛状态：%s" % ctx["status_text"])
        lines.append("")

        for c in list(comps)[:MAX_CANDIDATES_FOR_LLM]:
            trend_txt = ""
            if c.trend_n:
                trend_txt = "，走势 %s %.2f%%（%d 次变动）" % (
                    {"down": "降赔", "up": "升赔"}.get(c.trend, "未变"),
                    c.trend_pct, c.trend_n)
            lines.append("盘口 %s（水钱 %.2f%%%s）：" % (
                c.market, c.margin * 100, trend_txt))
            order = sorted(range(len(c.outcomes)),
                           key=lambda i: c.p_fair[i], reverse=True)
            for i in order[:MAX_OUTCOMES_PER_MARKET]:
                lines.append(
                    "  - %s：赔率 %.3f，市场公平概率 %.4f"
                    % (c.outcomes[i], c.odds[i], c.p_fair[i]))
        lines.append("")
        lines.append("降赔=资金流入=市场认为该结果概率上升。")
        lines.append("给出你的概率估计（可偏离市场）。只输出 JSON。")
        return "\n".join(lines)

    # -- 第 3 步：LLM 裁定 --------------------------------------------------

    def _call_llm_bounded(self, prompt: str) -> Any:
        """带**硬超时**的 LLM 调用。

        为何需要：实测推理型模型在较大输入上思考时间不可预测
        （同一 prompt 可 8s 也可 295s）。`urllib` 的 timeout 只管单次读，
        管不住模型侧的长思考，所以这里用线程 + join 做硬超时：
        超时立即返回失败，不让一个慢请求拖住整批决策。
        """
        from concurrent.futures import ThreadPoolExecutor

        assert self.llm is not None
        budget = self.config.llm_timeout_s
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(self.llm.complete_json, prompt,
                              system=MATCH_SYSTEM_PROMPT)
            try:
                return fut.result(timeout=budget)
            except TimeoutError as exc:
                # 不取消底层请求（让它自然结束），仅放弃等待
                raise LLMError(
                    "LLM 决策超时（>%.0fs）。推理型模型对该输入思考过久；"
                    "可减少盘口数或调大 llm_timeout_s。" % budget) from exc

    def _ask_llm(
        self,
        comps: Sequence[MarketComputation],
        home: str,
        away: str,
        league: str,
        context: Optional[Mapping[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], float, str, str]:
        """问 LLM 要**独立概率**，然后由系统算出优势（edge）。

        为何不是让 LLM 直接“挑一个”：
        小模型的概率来自去水，而**去水后的概率就是市场概率**，
        所以小模型的 edge 必然 ≤ 0（报告 §8.2）。若只让 LLM 从
        “已有 edge”里挑，就永远挑不出任何东西——系统名存实亡。

        正确做法（也是用户要求的）：LLM 给出**独立于市场的概率估计**，
        系统用它重算 edge。若 LLM 没有独立看法而照抄市场，
        edge 自然为负 → 不推荐买入，这也是合法结果。
        """
        if not self.llm_available:
            return [], 0.0, "", "LLM 未启用"
        prompt = self.build_prompt(comps, home, away, league, context)
        try:
            self.stats["llm_calls"] += 1
            assert self.llm is not None
            data = self._call_llm_bounded(prompt)
        except LLMError as exc:
            self.stats["llm_failures"] += 1
            return [], 0.0, "", "LLM 调用失败：%s" % str(exc)[:200]
        except (LLMNotConfigured, TimeoutError) as exc:
            self.stats["llm_failures"] += 1
            return [], 0.0, "", "LLM 调用失败：%s" % str(exc)[:200]

        if not isinstance(data, Mapping):
            self.stats["llm_failures"] += 1
            return [], 0.0, "", "LLM 返回结构不是对象"

        rows = data.get("markets")
        if rows is None:
            # 兼容旧格式（picks）：仅做解读提示，不再用它做买入依据
            rows = []
        if not isinstance(rows, Sequence) or isinstance(rows, str):
            self.stats["llm_failures"] += 1
            return [], 0.0, "", "LLM 未返回 markets 数组"

        by_market = {c.market: c for c in comps}
        picks: List[Dict[str, Any]] = []
        confs: List[float] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            mkt = str(row.get("market") or "").strip()
            comp = by_market.get(mkt)
            if comp is None:
                continue  # 幻觉盘口：直接丢弃
            raw = row.get("probabilities")
            if not isinstance(raw, Mapping):
                continue
            # 必须给出该盘口的**全部**结果，否则无法计算优势
            probs: List[float] = []
            ok = True
            for oc in comp.outcomes:
                v = raw.get(oc)
                if v is None or isinstance(v, bool):
                    ok = False
                    break
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    ok = False
                    break
                if not (0.0 <= fv <= 1.0):
                    ok = False
                    break
                probs.append(fv)
            if not ok:
                continue
            total = math.fsum(probs)
            if total <= 0:
                continue
            probs = [x / total for x in probs]

            try:
                conf = float(row.get("confidence", 0.0))
            except (TypeError, ValueError):
                conf = 0.0
            conf = min(1.0, max(0.0, conf))
            if conf < self.config.min_confidence:
                continue

            # 用 LLM 的概率**重新算** edge（这才是真实优势来源）
            for i, oc in enumerate(comp.outcomes):
                edge = probs[i] * comp.odds[i] - 1.0
                if edge < self.config.min_edge:
                    continue
                kelly = econ.fractional_kelly(
                    probs[i], comp.odds[i], lam=self.config.kelly_fraction)
                kelly = max(0.0, min(kelly, self.config.max_stake_pct))
                picks.append({
                    "market": mkt,
                    "outcome": oc,
                    "odds": round(comp.odds[i], 4),
                    "edge": round(edge, 6),
                    "edge_pct": round(edge * 100, 3),
                    "kelly": round(kelly, 6),
                    "kelly_pct": round(kelly * 100, 4),
                    "confidence": round(conf, 4),
                    "reason": str(row.get("reason") or "")[:200],
                    # 可复核性：三套概率全部回传
                    "p_market": round(comp.p_fair[i], 6),
                    "p_llm": round(probs[i], 6),
                    "trend": comp.trend,
                    "trend_pct": round(comp.trend_pct, 3),
                })
                confs.append(conf)

        picks.sort(key=lambda x: x["edge"] * x["confidence"], reverse=True)
        picks = picks[:MAX_PICKS]
        avg_conf = sum(confs) / len(confs) if confs else 0.0
        note = str(data.get("note") or "")[:240]
        return picks, avg_conf, note, ""

    # -- 主入口 -------------------------------------------------------------

    def decide_match(
        self,
        snapshots: Sequence[OddsSnapshot],
        trend: Optional[Mapping[str, Any]] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> MatchPicks:
        """一场比赛：经济学算全部盘口 → 一次 LLM → 买入建议。"""
        t0 = time.time()
        snaps = list(snapshots)
        res = MatchPicks(match_id=snaps[0].match_id if snaps else "",
                         n_markets=len(snaps))
        if snaps:
            res.league = snaps[0].league
            res.home = snaps[0].home
            res.away = snaps[0].away

        # 第 1 步：经济学算法（快，无网络）
        res.computations = self.compute_markets(snaps, trend)
        if not res.computations:
            res.decision = DECISION_AVOID
            res.error = "无可用盘口（全部停盘/下架或无快照）"
            res.elapsed_ms = int((time.time() - t0) * 1000)
            return res

        # 第 2/3 步：汇总给 LLM 做一次裁定
        picks, conf, note, reason = self._ask_llm(
            res.computations, res.home, res.away, res.league, context)
        res.picks = picks
        res.llm_confidence = conf
        res.llm_note = note
        res.llm_reason = reason
        res.llm_used = bool(self.llm_available and not reason)

        if not self.llm_available or reason:
            # 诚实降级：没有独立裁定就不给买入建议
            res.decision = DECISION_NO_LLM
            self.stats["no_llm"] += 1
        elif picks:
            res.decision = DECISION_BUY
            self.stats["with_picks"] += 1
            self.stats["picks"] += len(picks)
        else:
            # LLM 明确认为无值得买入的项 —— 这是合法结论
            res.decision = DECISION_AVOID

        res.elapsed_ms = int((time.time() - t0) * 1000)
        self.stats["matches"] += 1
        return res

    def decide_many(
        self,
        items: Sequence[Tuple[str, str, str, str, Sequence[OddsSnapshot],
                              Optional[Mapping[str, Any]], Optional[Mapping[str, Any]]]],
        max_workers: int = 4,
    ) -> List[MatchPicks]:
        """多场比赛**并行**决策（LLM 调用是网络等待，并行收益显著）。

        Args:
            items: `(match_id, league, home, away, snapshots, trend, context)`
            max_workers: 并行度（受推理服务承载能力限制）

        Returns:
            按 `rank_score` 降序（有买入建议的排前面）。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        out: List[MatchPicks] = []

        def _one(it: Any) -> MatchPicks:
            _mid, _lg, _h, _a, snaps, trend, ctx = it
            return self.decide_match(snaps, trend=trend, context=ctx)

        workers = max(1, min(_as_int(max_workers, DEFAULT_MAX_WORKERS),
                             len(items) or 1))
        if len(items) <= 1 or workers <= 1:
            for it in items:
                # 单场失败不能中断整批（与并行分支保持一致）
                try:
                    out.append(_one(it))
                except Exception as exc:  # noqa: BLE001
                    res = MatchPicks(match_id=str(it[0]), error=str(exc)[:200])
                    res.decision = DECISION_AVOID
                    out.append(res)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = [pool.submit(_one, it) for it in items]
                for f in as_completed(futs):
                    try:
                        out.append(f.result())
                    except Exception as exc:  # noqa: BLE001 - 单场失败不中断整批
                        res = MatchPicks(match_id="?", error=str(exc)[:200])
                        res.decision = DECISION_AVOID
                        out.append(res)
        out.sort(key=lambda r: r.rank_score, reverse=True)
        return out

    def health(self) -> Dict[str, Any]:
        return {
            "stats": dict(self.stats),
            "llm": self.llm_health(),
            "config": {
                "min_edge": self.config.min_edge,
                "min_confidence": self.config.min_confidence,
                "kelly_fraction": self.config.kelly_fraction,
                "max_llm_weight": self.config.max_llm_weight,
                "use_llm": self.config.use_llm,
            },
        }
