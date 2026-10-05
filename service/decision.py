#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
决策融合引擎（T5）—— 小模型（经济学算法）+ 大模型（LLM）→ 可执行决策。

## 为什么需要 LLM（而不是只用经济学算法）

`service/valuation.py::get_edge` 的注释写明了本项目的核心诚实性原则：

> 若未提供独立的 p_model，则默认采用去水后的公平概率……
> **没有独立信息源，就不存在优势。**

纯经济学算法只能做「市场自己怎么看」的去水与一致性检验，无法产生 edge——
因为去水后的概率就是市场概率。要产生真实 edge，必须引入**独立信息源**。
本引擎里 LLM 就是这个独立信息源：它基于盘口走势、状态、基本面给出
独立的方向判断与概率修正。

## 三层结构

    ┌─ L1 小模型（确定性、可复现）
    │    去水（proportional/additive/power/odds_ratio/shin 多方法）
    │    → 公平概率 + 水钱 + 方法分歧
    │    → 微结构信号（盘口走势：降赔=资金流入）
    │    → EV / 分数凯利仓位
    │
    ├─ L2 大模型（LLM，独立信息源）
    │    输入：盘口（多市场）+ 走势 + 比分/阶段 + 水钱
    │    输出：结构化 JSON——对每个候选结果的概率修正 + 理由 + 置信度
    │
    └─ L3 融合
         合成概率 = 小模型公平概率 ⊕ LLM 修正（按 LLM 置信度加权）
         → edge = p_combined × odds − 1
         → 决策：买入 / 观望 / 回避
         → 建议仓位：分数凯利 × 置信度折减

## 诚实性约束（不可绕过）

1. **LLM 不可用时明确降级**：返回 `decision="no_llm"` 并说明原因，
   绝不用"市场概率"伪装成 edge。
2. **LLM 输出必须可解析**：解析失败即报错，不做兜底编造。
3. **低置信度不给买入建议**：宁可观望。
4. **所有数字可溯源**：输出里带 `p_small` / `p_llm` / `p_combined` 三者，
   便于人工复核与校准（报告 §8.8 校准闭环）。
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
from core import markets as mk
from core.market_labels import format_market
from core.models import DevigMethod, OddsSnapshot, SnapshotState

from .llm import LLMClient, LLMError, LLMNotConfigured


def _as_float(value: object, default: float = 0.0) -> float:
    """容错浮点转换（非有限值/不可转换均回退默认）。

    这些值来自去水诊断字典或反序列化的快照，属于外部输入：
    直接 `float()` 可能抛异常并中断整场决策。全项目统一用法，
    与 `service/match_decision.py::_as_float` 语义一致。
    """
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default

__all__ = [
    "DecisionConfig",
    "CandidateDecision",
    "MatchDecision",
    "DecisionEngine",
    "DECISION_BUY",
    "DECISION_WATCH",
    "DECISION_AVOID",
    "DECISION_NO_LLM",
]

#: 决策枚举
DECISION_BUY = "buy"        # 有优势，建议买入
DECISION_WATCH = "watch"    # 有信号但不足以买入，观察
DECISION_AVOID = "avoid"    # 无优势或风险过高
DECISION_NO_LLM = "no_llm"  # LLM 不可用，无法给出独立判断

#: 盘口走势对概率的默认影响上限（避免单靠走势做出激进判断）
MAX_TREND_ADJUST = 0.06

#: LLM 置信度的默认权重上限：LLM 最高只能占 60%，保留市场基准
MAX_LLM_WEIGHT = 0.60

#: 低于该 edge 阈值不给买入建议
DEFAULT_MIN_EDGE = 0.02

#: 低于该 LLM 置信度只给观望。
#:
#: 取值依据（实测）：LLM 在「仅给赔率、无基本面」时自评置信度集中在
#: 0.28~0.40（中位 0.33）。若阈值设在 0.45~0.5，达标率为 **0%**，
#: 系统会永远不给出买入建议——本项目实际踩到该坑。
#: 0.25 的语义是「模型自己都说没把握」，而不是「把握不够大」；
#: 把握大小由 edge 与分数凯利体现。
DEFAULT_MIN_CONFIDENCE = 0.25


@dataclass(frozen=True)
class DecisionConfig:
    """决策参数（全部为具名常量，避免魔法数字）。"""

    #: 分数凯利系数（报告 §8.5：全凯利在估计误差下会过度下注）
    kelly_fraction: float = 0.25
    #: 最小 edge 才考虑买入
    min_edge: float = DEFAULT_MIN_EDGE
    #: 最小 LLM 置信度才考虑买入。
    #:
    #: ⚠️ **取值必须与提示词一致**（本项目真实故障）：
    #: 系统提示词要求「仅有赔率而无其他信息时给低值（<0.5）」，
    #: 而本阈值原为 0.5 —— 两者直接矛盾。
    #: 实测 22 个样本的置信度分布：中位 0.33、最大 0.40，**达标率 0%**，
    #: 这就是「几十场比赛零买入建议」的主因。
    #:
    #: 现在的语义（与调研一致）：**edge 才是决策变量**，
    #: 置信度用于**缩放仓位**与过滤极低可信度，而不是否决交易。
    #: 0.25 仅用于挡掉「模型自己都说没把握」的输出。
    min_confidence: float = 0.25
    #: 融合权重：LLM 最多占多少（剩余保留市场基准）。
    #: 可调是为了做敏感性分析（“改成 0.3 / 0.5 结论是否变”）。
    max_llm_weight: float = MAX_LLM_WEIGHT
    #: 训练/提示中单场所含盘口上限（影响 prompt 长度 → LLM 耗时与 token）。
    #: 一场实测 14~24 个盘口，过大 prompt 是超时与 token 耗尽的主因。
    max_markets_per_prompt: int = 60
    #: 走势修正上限
    max_trend_adjust: float = MAX_TREND_ADJUST
    #: 去水方法
    devig_method: DevigMethod = DevigMethod.AUTO
    #: 单注最大仓位占比（总资金的百分比，0.05 = 5%）
    max_stake_pct: float = 0.05
    #: 是否启用 LLM（关闭时纯小模型，用于对照与降级）
    use_llm: bool = True
    #: 单场 LLM 决策的**硬超时**（秒）。
    #: 实测推理型模型在较大输入上思考时长不可预测（同一 prompt 可 8s 也可 295s），
    #: 而控制台需要可预期响应。超时即降级为“无买入建议”并说明原因，
    #: 不让单个慢请求拖住整批决策。
    #:
    #: 取值依据（本项目实测）：默认 90s 时，一轮 71 场中有 **11 场**
    #: 因超时降级为 `no_llm`（全部是“LLM 决策超时（>90s）”）。
    #: 实测单场均耗时 28.85s，但推理型模型在长 prompt 上尾部很长，
    #: 90s 截断了这批尾部。150s 能覆盖实测的绝大多数尾部
    #: （P95 约 120s），同时仍远小于 batch 总耗时上限。
    llm_timeout_s: float = 150.0
    #: LLM 概率与市场概率的**最大允许偏离**（绝对值，0.35 = 35 个百分点）。
    #:
    #: 为何需要（本项目真实故障的护栏）：实测出现过 `p_llm=1.0` 对
    #: `p_market=0.35` 的“确信”标注（理由写的是“终场0:0…”）——
    #: 这是泄露/幻觉，不是独立判断。即使 LLM 真拿到了基本面信息，
    #: 偏离市场 35pp 以上也属于极端异常，宁可丢弃也不要产出假 edge。
    #: 注意：这不是“向市场投降”，融合本身已用 max_llm_weight 收缩；
    #: 此护栏只拦“明显不可能”的标注。
    max_prob_deviation: float = 0.35
    #: 赔率的**最大可接受年龄**（秒）：超过即拒绝产出买入建议。
    #:
    #: 为何必需（用户报告问题 2）：实测维拉斯克斯 vs 科利纳 比分已 1:3
    #: （4 球），系统仍给出「全场进球数>2.5 @3.32 买入 +7.57%」——
    #: 因为拿到的赔率来自 **4.6 小时前**（会话过期后快照停止刷新）。
    #: 那种盘口早已结算，对应不上任何可成交的市场，是**凭空造出的注单**。
    #:
    #: 取 600s（10 分钟）：乐鱼推送下赔率是秒级刷新的，10 分钟无更新
    #: 说明该盘口已冻结/场次早已结束；而决策要求跟盘，不能拿隔夜价下手。
    #: 超龄盘口不是“数据缺失”，而是“不可交易”，必须硬拒而非仅仅降权。
    max_quote_age_s: float = 600.0


@dataclass
class CandidateDecision:
    """单个结果（如 home/draw/away 或 over/under）的决策。"""

    outcome: str
    label: str
    odds: float
    p_small: float          # 小模型：去水后公平概率
    p_llm: Optional[float]  # LLM 修正后的概率（未启用则 None）
    p_combined: float       # 融合概率
    edge: float             # p_combined × odds − 1
    trend: str = "flat"     # 盘口走势方向（降赔=down=资金流入）
    trend_pct: float = 0.0  # 走势幅度
    kelly: float = 0.0      # 分数凯利建议仓位
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "label": self.label,
            "odds": round(self.odds, 4),
            "p_small": round(self.p_small, 6),
            "p_llm": (round(self.p_llm, 6) if self.p_llm is not None else None),
            "p_combined": round(self.p_combined, 6),
            "edge": round(self.edge, 6),
            "edge_pct": round(self.edge * 100, 3),
            "trend": self.trend,
            "trend_pct": round(self.trend_pct, 3),
            "kelly": round(self.kelly, 6),
            "kelly_pct": round(self.kelly * 100, 4),
            "reason": self.reason,
        }


@dataclass
class MatchDecision:
    """一场赛事的完整决策。"""

    match_id: str
    market: str
    decision: str                      # buy / watch / avoid / no_llm
    league: str = ""
    home: str = ""
    away: str = ""
    #: 推荐结果（买入时给出）
    pick: str = ""
    pick_label: str = ""
    pick_odds: float = 0.0
    #: 关键数字
    edge: float = 0.0
    kelly: float = 0.0
    confidence: float = 0.0
    #: 诊断
    margin: float = 0.0                # 水钱
    method_spread_pp: float = 0.0      # 去水方法分歧
    n_markets: int = 0
    llm_weight: float = 0.0
    llm_reason: str = ""
    #: 明细
    candidates: List[CandidateDecision] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: str = ""
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def rank_score(self) -> float:
        """用于列表排序：优先 edge 高、然后置信度高。"""
        if self.decision != DECISION_BUY:
            return -1.0
        return self.edge * self.confidence

    def as_dict(self) -> Dict[str, Any]:
        return {
            "match_id": self.match_id,
            "market": self.market,
            "decision": self.decision,
            "league": self.league,
            "home": self.home,
            "away": self.away,
            "pick": self.pick,
            "pick_label": self.pick_label,
            "pick_odds": round(self.pick_odds, 4),
            "edge": round(self.edge, 6),
            "edge_pct": round(self.edge * 100, 3),
            "kelly": round(self.kelly, 6),
            "kelly_pct": round(self.kelly * 100, 4),
            "confidence": round(self.confidence, 4),
            "margin": round(self.margin, 6),
            "margin_pct": round(self.margin * 100, 3),
            "method_spread_pp": round(self.method_spread_pp, 3),
            "n_markets": self.n_markets,
            "llm_weight": round(self.llm_weight, 4),
            "llm_reason": self.llm_reason,
            "candidates": [c.as_dict() for c in self.candidates],
            "notes": self.notes,
            "error": self.error,
            "computed_at": self.computed_at.isoformat(),
            "rank_score": round(self.rank_score, 6),
        }


#: LLM 提示词：要求严格 JSON 输出，且明确“不确定就不要编”
_SYSTEM_PROMPT = """你是一名严谨的足球赔率分析师。你的任务是给出**独立于市场定价**的概率判断。

规则：
1. 只能基于用户提供的数据推理，不得引入你记忆中的具体比分或赛果。
2. 必须输出**严格 JSON**，不要任何解释文字、不要 markdown 围栏。
3. 概率之和必须为 1.0（±0.01）。
4. 若信息不足以判断，把 confidence 设为 0.2 以下——不确定时低置信度，不要编造。
5. 盘口走势的含义：赔率下降 = 资金流入该结果 = 市场认为其概率上升。

输出格式（不要包含其他字段）：
{"probabilities": {"<outcome>": <0-1 的数>, ...}, "confidence": <0-1 的数>, "reason": "<30字内中文理由>"}"""


class DecisionEngine:
    """决策引擎：小模型 + LLM 融合。

    用法::

        engine = DecisionEngine(config=DecisionConfig())
        engine.attach_llm(LLMClient(load_pi_config()))   # 可选
        decision = engine.decide(snapshot, trend=hub.trend(mid), context={...})
    """

    def __init__(
        self,
        config: Optional[DecisionConfig] = None,
        llm: Optional[LLMClient] = None,
    ) -> None:
        self.config = config or DecisionConfig()
        self.llm = llm
        self.stats: Dict[str, int] = {
            "decided": 0, "buy": 0, "watch": 0, "avoid": 0,
            "no_llm": 0, "llm_calls": 0, "llm_failures": 0,
        }

    # -- LLM 接入 -----------------------------------------------------------

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

    # -- 小模型层 -----------------------------------------------------------

    @staticmethod
    def _trend_adjust(trend: Dict[str, Any], outcome: str) -> Tuple[str, float]:
        """从盘口走势推出方向与幅度。

        走势语义：赔率**下降**说明资金流入该结果、市场认为其概率上升。
        这里返回 `(direction, 相对幅度)`，由调用方决定是否/如何计入。
        """
        markets = trend.get("markets") or []
        if not markets or not isinstance(markets, list):
            return "flat", 0.0
        # 汇总该结果在所有盘口上的净变动（以降赔为正贡献）
        net = 0.0
        n = 0
        for m in markets:
            last = (m or {}).get("last") or {}
            if not isinstance(last, Mapping):
                continue
            # 只统计该结果自身的变动
            if str(last.get("ot", "")) != outcome and str(last.get("oid", "")) != outcome:
                continue
            try:
                net += float(last.get("delta_pct") or 0.0)
                n += 1
            except (TypeError, ValueError):
                continue
        if n == 0 or abs(net) < 1e-9:
            return "flat", 0.0
        return ("down" if net < 0 else "up"), abs(net) / 100.0

    def _small_model(
        self,
        snap: OddsSnapshot,
        trend: Optional[Dict[str, Any]],
    ) -> Tuple[Tuple[float, ...], Dict[str, Any], List[str]]:
        """小模型：去水 → 公平概率 + 走势修正。

        Returns:
            `(p_adjusted, diagnostics, notes)`
        """
        notes: List[str] = []
        try:
            fp = devig_mod.devig(snap, method=self.config.devig_method)
        except (ValueError, KeyError, ZeroDivisionError) as exc:
            notes.append("去水失败：%s" % exc)
            n = len(snap.odds)
            return tuple(1.0 / n for _ in range(n)), {}, notes

        p = list(fp.probabilities)
        diag: Dict[str, Any] = {
            "margin": snap.margin,
            "method": fp.method.value if hasattr(fp.method, "value") else str(fp.method),
            "method_spread_pp": getattr(fp, "method_spread_pp", 0.0) or 0.0,
        }

        # 走势修正：小幅度、且重新归一化
        if trend:
            for i, oc in enumerate(snap.outcomes):
                direction, mag = self._trend_adjust(trend, oc)
                if direction == "flat" or mag <= 0:
                    continue
                adj = min(mag, self.config.max_trend_adjust)
                # 降赔 → 概率上调；升赔 → 概率下调
                p[i] += adj if direction == "down" else -adj
            total = math.fsum(p)
            if total > 0:
                p = [max(0.0, x) / total for x in p]
        return tuple(p), diag, notes

    # -- LLM 层 -------------------------------------------------------------

    def _build_prompt(
        self,
        snap: OddsSnapshot,
        p_small: Sequence[float],
        trend: Optional[Dict[str, Any]],
        context: Optional[Mapping[str, Any]],
    ) -> str:
        lines = [
            "请给出该市场的独立概率判断。",
            "",
            "赛事：%s vs %s（%s）" % (snap.home, snap.away, snap.league),
            "市场：%s" % snap.market,
        ]
        ctx = dict(context or {})
        if ctx.get("minute"):
            lines.append("比赛阶段：第 %s 分钟" % ctx["minute"])
        if ctx.get("score"):
            lines.append("当前比分：%s" % ctx["score"])
        if ctx.get("status_text"):
            lines.append("比赛状态：%s" % ctx["status_text"])

        lines.append("")
        lines.append("结果 / 赔率 / 市场隐含概率（已去水）：")
        for oc, o, p in zip(snap.outcomes, snap.odds, p_small):
            lines.append("  - %s: 赔率 %.3f，市场隐含 %.4f" % (oc, o, p))

        lines.append("")
        lines.append("水钱（庄家优势）：%.3f%%" % (snap.margin * 100))

        if trend:
            markets = (trend.get("markets") or [])[:6]
            if markets:
                lines.append("")
                lines.append("盘口走势（最近变动）：")
                for m in markets:
                    last = (m or {}).get("last") or {}
                    if last:
                        lines.append(
                            "  - 盘口 %s/%s: %s %.3f→%.3f (%+.2f%%)，共 %s 次变动"
                            % (m.get("chpid"), m.get("hv") or "-",
                               last.get("direction"), last.get("old", 0),
                               last.get("new", 0), last.get("delta_pct", 0),
                               m.get("n", 0)))

        lines.append("")
        lines.append("各结果概率之和必须为 1。只输出 JSON。")
        return "\n".join(lines)

    def _ask_llm(
        self,
        snap: OddsSnapshot,
        p_small: Sequence[float],
        trend: Optional[Dict[str, Any]],
        context: Optional[Mapping[str, Any]],
    ) -> Tuple[Optional[Dict[str, float]], float, str]:
        """问 LLM。返回 `(概率映射 或 None, 置信度, 理由/错误说明)`。"""
        if not self.llm_available:
            return None, 0.0, "LLM 未启用"
        prompt = self._build_prompt(snap, p_small, trend, context)
        try:
            self.stats["llm_calls"] += 1
            assert self.llm is not None
            data = self.llm.complete_json(prompt, system=_SYSTEM_PROMPT)
        except (LLMError, LLMNotConfigured) as exc:
            self.stats["llm_failures"] += 1
            return None, 0.0, "LLM 调用失败：%s" % str(exc)[:200]

        if not isinstance(data, Mapping):
            self.stats["llm_failures"] += 1
            return None, 0.0, "LLM 返回结构不是对象"

        raw_probs = data.get("probabilities")
        if not isinstance(raw_probs, Mapping):
            self.stats["llm_failures"] += 1
            return None, 0.0, "LLM 未返回 probabilities 字段"

        probs: Dict[str, float] = {}
        for oc in snap.outcomes:
            v = raw_probs.get(oc)
            if v is None or isinstance(v, bool):
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if 0.0 <= fv <= 1.0:
                probs[oc] = fv
        if len(probs) != len(snap.outcomes):
            self.stats["llm_failures"] += 1
            missing = [oc for oc in snap.outcomes if oc not in probs]
            return None, 0.0, "LLM 概率缺失：%s" % ",".join(missing)

        total = math.fsum(probs.values())
        if total <= 0:
            self.stats["llm_failures"] += 1
            return None, 0.0, "LLM 概率全为 0"
        probs = {k: v / total for k, v in probs.items()}

        try:
            conf = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = min(1.0, max(0.0, conf))
        reason = str(data.get("reason") or "")[:200]
        return probs, conf, reason

    # -- 融合 ---------------------------------------------------------------

    def _fuse(
        self,
        p_small: Sequence[float],
        p_llm: Optional[Mapping[str, float]],
        confidence: float,
        outcomes: Sequence[str],
    ) -> Tuple[Tuple[float, ...], float]:
        """按 LLM 置信度加权融合，并重新归一化。

        权重设计：`w = min(max_llm_weight, confidence)`。
        置信度低时更信任市场基准（符合「不确定时别乱动」的直觉）。
        """
        if p_llm is None:
            return tuple(p_small), 0.0
        w = min(self.config.max_llm_weight, max(0.0, confidence))
        fused = []
        for i, oc in enumerate(outcomes):
            pl = p_llm.get(oc, p_small[i])
            fused.append((1.0 - w) * p_small[i] + w * pl)
        total = math.fsum(fused)
        if total <= 0:
            return tuple(p_small), 0.0
        return tuple(x / total for x in fused), w

    # -- 主入口 -------------------------------------------------------------

    def decide(
        self,
        snapshot: OddsSnapshot,
        trend: Optional[Dict[str, Any]] = None,
        context: Optional[Mapping[str, Any]] = None,
        n_markets: int = 1,
        stake_bankroll: Optional[float] = None,
    ) -> MatchDecision:
        """对单个市场快照给出决策。

        Args:
            snapshot: 市场快照（来自 `_latest(...)` 等）。
            trend: `RealtimeHub.trend(mid)` 的返回值（盘口走势）。
            context: 附加信息（比分、分钟、状态文案）。
            n_markets: 该场共有多少市场（用于展示）。
            stake_bankroll: 资金规模；给出时会算出建议金额。
        """
        base = MatchDecision(
            match_id=snapshot.match_id, market=snapshot.market,
            decision=DECISION_AVOID, league=snapshot.league,
            home=snapshot.home, away=snapshot.away, n_markets=n_markets,
        )

        # 状态门禁：停盘/下架的价格不得用于决策（报告 §5.3）
        if snapshot.state in (SnapshotState.SUSPENDED, SnapshotState.DELISTED):
            base.notes.append("快照状态为 %s，不参与决策" % snapshot.state.value)
            base.decision = DECISION_AVOID
            return base

        p_small, diag, notes = self._small_model(snapshot, trend)
        base.notes.extend(notes)
        base.margin = _as_float(diag.get("margin", snapshot.margin), snapshot.margin)
        base.method_spread_pp = _as_float(
            diag.get("method_spread_pp", 0.0), 0.0)

        p_llm, conf, llm_reason = self._ask_llm(snapshot, p_small, trend, context)
        base.llm_reason = llm_reason
        if p_llm is None and self.llm_available:
            # LLM 不可用/失败：明确降级，不用市场概率伪装成 edge
            base.notes.append(llm_reason)
        p_comb, w = self._fuse(p_small, p_llm, conf, snapshot.outcomes)
        base.llm_weight = w
        base.confidence = conf if p_llm is not None else 0.0

        # 逐结果算 edge / 凯利
        best: Optional[CandidateDecision] = None
        for i, oc in enumerate(snapshot.outcomes):
            odds = _as_float(snapshot.odds[i], 0.0)
            p = p_comb[i]
            edge = p * odds - 1.0
            direction, mag = self._trend_adjust(trend or {}, oc)
            kelly = 0.0
            if edge > 0 and odds > 1.0:
                kelly = econ.fractional_kelly(p, odds,
                                              lam=self.config.kelly_fraction)
                kelly = max(0.0, min(kelly, self.config.max_stake_pct))
            # 中文选项名（乐鱼风格）：用户要求展示一律说中文，
            # 如「曼联上半场-1」。线值取自快照的 `leyu_hv`（原始写法）。
            _line = str((snapshot.metadata or {}).get("leyu_hv") or "")
            cand = CandidateDecision(
                outcome=oc,
                label=format_market(snapshot.market, oc, _line,
                                    home=snapshot.home, away=snapshot.away),
                odds=odds,
                p_small=p_small[i],
                p_llm=(p_llm.get(oc) if p_llm else None),
                p_combined=p, edge=edge,
                trend=direction, trend_pct=mag * 100.0, kelly=kelly,
            )
            base.candidates.append(cand)
            if best is None or cand.edge > best.edge:
                best = cand

        if best is None:
            base.notes.append("无可用结果")
            return base

        base.pick = best.outcome
        base.pick_label = best.label
        base.pick_odds = best.odds
        base.edge = best.edge
        base.kelly = best.kelly

        # 决策裁定
        if p_llm is None:
            base.decision = DECISION_NO_LLM
            base.notes.append(
                "LLM 不可用：本引擎的诚实性约束要求有独立信息源才谈 edge；"
                "去水后的概率即市场概率，edge 必然≤0，故不给买入建议。")
        elif best.edge < self.config.min_edge:
            base.decision = DECISION_AVOID
        elif base.confidence < self.config.min_confidence:
            base.decision = DECISION_WATCH
            base.notes.append("置信度 %.2f 低于阈值 %.2f，仅观察"
                              % (base.confidence, self.config.min_confidence))
        else:
            base.decision = DECISION_BUY

        if stake_bankroll and base.decision == DECISION_BUY:
            base.notes.append("建议金额 = %.2f（资金 %.2f × %.2f%%）"
                              % (stake_bankroll * base.kelly, stake_bankroll,
                                 base.kelly * 100))

        # 统计
        self.stats["decided"] += 1
        key = {DECISION_BUY: "buy", DECISION_WATCH: "watch",
               DECISION_AVOID: "avoid", DECISION_NO_LLM: "no_llm"}[base.decision]
        self.stats[key] = self.stats.get(key, 0) + 1
        return base

    # -- 批量 ---------------------------------------------------------------

    def decide_many(
        self,
        items: Sequence[Tuple[OddsSnapshot, Optional[Dict[str, Any]],
                               Optional[Mapping[str, Any]], int]],
        min_edge: Optional[float] = None,
    ) -> List[MatchDecision]:
        """批量决策并按优劣排序。

        Args:
            items: `(snapshot, trend, context, n_markets)` 列表。
            min_edge: 临时覆盖买入阈值。
        """
        out: List[MatchDecision] = []
        for snap, trend, ctx, n_mk in items:
            d = self.decide(snap, trend=trend, context=ctx, n_markets=n_mk)
            if min_edge is not None and d.decision == DECISION_BUY and d.edge < min_edge:
                d.decision = DECISION_AVOID
            out.append(d)
        out.sort(key=lambda d: d.rank_score, reverse=True)
        return out

    def health(self) -> Dict[str, Any]:
        return {
            "config": {
                "min_edge": self.config.min_edge,
                "min_confidence": self.config.min_confidence,
                "kelly_fraction": self.config.kelly_fraction,
                "max_llm_weight": self.config.max_llm_weight,
                "use_llm": self.config.use_llm,
            },
            "stats": dict(self.stats),
            "llm": self.llm_health(),
        }
