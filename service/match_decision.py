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
from core.entry_gate import (
    EntryGateConfig,
    GateResult,
    evaluate_entry,
    gate_market,
)
from core.market_labels import describe_market, format_market
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
    "MAX_MARKETS_PER_PROMPT",
    "MarketComputation",
    "MatchPicks",
    "MatchDecisionEngine",
    "MATCH_SYSTEM_PROMPT",
]

#: 交给 LLM 的候选盘口上限。
#: 用户要求「经济学算法逐个分析每场比赛的每个盘口」——
#: 因此**不按数量截断**，而是带上全部盘口（一场实测 14~24 个）。
#: 这里只留一个防御性硬上限，防异常数据（如上千盘口）把上下文撑爆。
MAX_MARKETS_PER_PROMPT = 60

#: 单个盘口最多展示的结果数
MAX_OUTCOMES_PER_MARKET = 3

#: LLM 返回的买入建议上限（防止一次给出一堆"推荐"）
MAX_PICKS = 6

#: 默认并行度（受推理服务承载能力限制）
DEFAULT_MAX_WORKERS = 4

#: 单场 LLM 决策超时（秒）。
#: 实测推理型模型在较大输入上思考时间不可预测（曾出现 295s），
#: 而控制台需要可预期响应。超时即降级为“无买入建议”并说明原因，
#: 绝不让一个慢请求拖住整批决策。
DEFAULT_MATCH_LLM_TIMEOUT_S = 90.0


def _elapsed_ms(t0: float) -> int:
    """距 `t0` 的毫秒数（`time.time()` 理论上可因系统时钟回拨而异常，
    故用 try 包裹：耗时统计不应影响决策结果）。"""
    try:
        return int((time.time() - t0) * 1000)
    except (TypeError, ValueError, OverflowError, OSError):
        return 0


def _tick_age_s(ts_ms: object) -> Optional[float]:
    """最近一次盘口跳动距今秒数；无有效时间戳时返回 None。

    用于入场门控的「重定价窗口」判定（[C] 调研结论：进球后各盘口依次
    重定价，刚跳完价时价格尚未稳定）。
    """
    ms = _as_int(ts_ms, 0)
    if ms <= 0:
        return None
    age = time.time() - ms / 1000.0
    # 上游时间戳可能超前于本机时钟（时钟漂移），负数视为“刚刚发生”
    return max(0.0, age)


def _lookup_probability(raw: Mapping[str, Any], outcome: str,
                        market: str, line: str,
                        home: object = "", away: object = "") -> Optional[float]:
    """从 LLM 返回的概率字典里取某结果的值（容错多种键写法）。

    依次尝试：
      1. 内部代码（`home`/`over`）—— 提示词要求的标准写法；
      2. 中文标签（如「曼联上半场-1」「上半场进球数>1/1.5」）；
      3. 不带队名的中文标签（LLM 可能省略队名）；
      4. 大小写/空白不敏感匹配。

    为何必须容错：提示词里同时展示了中文标签，LLM 很容易照中文作答；
    若解析器只认内部代码，整个盘口会被**静默丢弃**，
    表现为「系统跑完却零买入建议」这类难查的故障。

    ⚠️ **`home`/`away` 必须与构建提示词时一致**：让球标签含队名
    （`曼联上半场-1`），若这里不传队名，就会回退成 `主队上半场-1`
    而永远匹配不上 LLM 的答案 —— 重现同一种静默丢盘故障。
    """
    if outcome in raw:
        return _to_prob(raw[outcome])

    # 候选标签：带队名（与提示词逐字一致）→ 不带队名（LLM 省略时兼容）
    candidates = [format_market(market, outcome, line,
                                home=home, away=away)]
    plain = format_market(market, outcome, line)
    if plain not in candidates:
        candidates.append(plain)

    for label in candidates:
        v = raw.get(label)
        if v is not None:
            return _to_prob(v)

    # 归一化比较（去空白、统一大小写）
    norm = {str(k).strip(): val for k, val in raw.items()}
    for key in [str(outcome).strip()] + [c.strip() for c in candidates]:
        if key in norm:
            return _to_prob(norm[key])
        low = {k.lower(): v for k, v in norm.items()}
        if key.lower() in low:
            return _to_prob(low[key.lower()])
    return None


def _to_prob(value: object) -> Optional[float]:
    """把 LLM 给的概率值转为 float；非法/布尔返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None


def _collect_rejects(comps: Sequence[MarketComputation]) -> List[str]:
    """汇总未通过门控的原因码（去重，保持首次出现顺序）。

    用途：让「为什么这场比赛没进 LLM」有可读的解释，
    而不是只给一个空结果。
    """
    out: List[str] = []
    for c in comps:
        for r in c.reject_reasons:
            if r not in out:
                out.append(r)
    return out


def _snap_age_s(snap: OddsSnapshot) -> Optional[float]:
    """快照年龄（秒）；`captured_at` 缺失/非法时返回 None。

    用于时效硬门禁（`DecisionConfig.max_quote_age_s`）：过期赔率算出的
    「优势」是虚假的 —— 实测一场 1:3（4 球）的比赛，因为取到 4.6 小时前
    的 3.32 而给出「全场进球数>2.5 买入」，而该盘口早已结算。

    上游时钟可能略快于本机，负数一律视为 0（“刚刚发生”）。
    """
    when = getattr(snap, "captured_at", None)
    if when is None:
        return None
    try:
        age = (datetime.now(timezone.utc) - when).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0.0, age)


def _latest_per_market(
    snapshots: Sequence[OddsSnapshot],
) -> List[OddsSnapshot]:
    """每个盘口只保留**最新**一份快照（按 `captured_at`）。

    为何必须（本项目真实严重缺陷）：快照库是**不可变历史**，一场比赛
    同一盘口会累积几十份不同时间的快照。实测 `5726509` 传入 216 条，
    其中 `OU(2.5)` 重复 **15** 次，赔率从 1.94 一路变到 3.32。
    不过滤就会：

      * 同一盘口被重复计算与重复上报（看板出现重复盘口）；
      * **挑到过期价格**：该场已 1:3（4 球），`全场进球数>2.5` 早已结算，
        却取到 1.8 小时前的赔率 3.32，给出「买入 +7.57%」的**假注单**。

    去重键用 `(market, chpid, hv)` 而非只用 `market`：
    乐鱼会把同一盘口的不同盘口线（如 `OU` 的 2.5 / 2.75 / 3）
    拆成独立的市场条目，它们的风险完全不同，**不能合并**。
    同一键下取 `captured_at` 最大者；时间相同时用列表后者（调用方
    一般已按时间升序传入，后者即更新）。

    Args:
        snapshots: 该场全部快照（可能含大量历史）。

    Returns:
        每键一份的最新快照列表，保持首次出现顺序（便于输出可复现）。
    """
    best: Dict[Tuple[str, str, str], OddsSnapshot] = {}
    for snap in snapshots:
        meta = snap.metadata or {}
        key = (str(snap.market),
               str(meta.get("leyu_chpid") or ""),
               str(meta.get("leyu_hv") or ""))
        prev = best.get(key)
        if prev is None or snap.captured_at >= prev.captured_at:
            best[key] = snap
    return list(best.values())


def _safe_odds(values: Sequence[object]) -> Tuple[float, ...]:
    """把赔率序列统一为 float（**长度保持不变**）。

    赔率理论上已在 `OddsSnapshot.__post_init__` 归一化为 float，
    但快照可能来自反序列化或外部构造，故此处再兜一层。
    非法值记为 0.0（后续 edge 计算会自然把它判为无优势），
    **不跳过元素**——跳过会让 edges 与 outcomes 下标错位。
    """
    out: List[float] = []
    for v in values:
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            f = 0.0
        out.append(f if math.isfinite(f) else 0.0)
    return tuple(out)


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
4. probabilities 的**键必须逐字照抄用户消息里的 `键=` 值**（如 `home`/`away`/`over`/`under`/`draw`），
   不要翻译、不要用中文描述、不要改大小写；键缺失或写错会导致该盘口被系统丢弃。
5. probabilities 必须含该盘口**全部结果**，总和为 1。
6. confidence 是你对**自己估计**的把握；仅有赔率而无其他信息时给低值（<0.5）。
7. market 字段必须逐字照抄用户消息里的 `[代码]`（如 `AH(0.25)`）。

输出：
{"markets":[{"market":"<代码>","probabilities":{"<结果键>":<0-1>},"confidence":<0-1>,"reason":"<20字内>"}]}"""


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
    #: **原始线值**（如 `0/0.5`、`1.5`）。
    #: 必须保留原始写法：盘口代码里的 `0.25` 只是复合盘（`0/0.5`）的中点近似，
    #: 展示中点会让人困惑，而中文标签（“上半场大1.5”）需要真实线值。
    line: str = ""
    state: str = SnapshotState.ACTIVE.value
    #: 走势摘要（该盘口）
    trend: str = "flat"
    trend_pct: float = 0.0
    trend_n: int = 0
    #: 逐结果的入场门控结论（经济学闸门）。
    #: 只有 `gates[i].passed` 为真的结果才交给 LLM 做最终裁定，
    #: 这是「经济学算法满足后才调用 LLM」的落地点。
    gates: Tuple[GateResult, ...] = ()
    #: 主/客队名。用于生成**乐鱼风格**的中文选项名
    #: （如「曼联上半场-1」）；缺失时回退为「主队/客队」。
    home: str = ""
    away: str = ""

    @property
    def gate_passed(self) -> bool:
        """该盘口是否有任一结果通过经济门控。"""
        return any(g.passed for g in self.gates)

    @property
    def gate_passed_indices(self) -> List[int]:
        """通过门控的结果下标（LLM 只看这些）。"""
        return [i for i, g in enumerate(self.gates) if g.passed]

    @property
    def best_gate(self) -> Optional[GateResult]:
        """通过门控且余量最大的结果；无则返回 None。"""
        ok = [g for g in self.gates if g.passed]
        return max(ok, key=lambda g: g.margin) if ok else None

    @property
    def reject_reasons(self) -> List[str]:
        """未通过门控的原因码（去重，供前端分组统计）。"""
        out: List[str] = []
        for g in self.gates:
            for r in g.rejects:
                if r not in out:
                    out.append(r)
        return out

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
            "market_label": describe_market(self.market),
            "line": self.line,
            "outcomes": list(self.outcomes),
            # 中文选项名（与 outcomes 同序）——用户要求展示与乐鱼一致：
            # 如「曼联上半场-1」「上半场进球数>1/1.5」。
            "outcome_labels": [
                format_market(self.market, oc, self.line,
                              home=self.home, away=self.away)
                for oc in self.outcomes],
            # 最优选项的中文名，便于列表/卡片直接展示
            "best_label": (
                format_market(self.market, self.outcomes[self.best_index],
                              self.line, home=self.home, away=self.away)
                if self.best_index >= 0 else ""),
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
            "gate_passed": self.gate_passed,
            "gate_passed_outcomes": [self.outcomes[i]
                                     for i in self.gate_passed_indices],
            "reject_reasons": self.reject_reasons,
            "gates": [g.as_dict() for g in self.gates],
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
    #: 入场门控统计：通过 / 未通过经济门槛的盘口数。
    #: 用户要求的工作逻辑是「经济算法满足后才调用 LLM」，
    #: 这两个数就是该逻辑可观测的证据（未通过即不进入 LLM）。
    gated_in: int = 0
    gated_out: int = 0
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
            # 门控结果：前端可直接展示「多少个盘口通过经济门槛」
            "gated_in": self.gated_in,
            "gated_out": self.gated_out,
            "reject_reasons": _collect_rejects(self.computations),
            "has_buy": self.has_buy,
            "picks": self.picks,
            # 便于列表直接展示：最优建议的中文描述
            "best_label": (best["pick_label"] if (best := self.best_pick) else ""),
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
                 llm: Optional[LLMClient] = None,
                 entry_config: Optional[EntryGateConfig] = None) -> None:
        self.config = config or DecisionConfig()
        #: 入场门控参数（经济学算法必须先通过才交给 LLM）。
        #: 与 `DecisionConfig` 分开：前者是「该不该看」，后者是「怎么看」。
        self.entry_config = entry_config or EntryGateConfig()
        #: 复用逐盘口引擎的小模型层（去水与走势修正逻辑只写一处）
        self.small = DecisionEngine(self.config, llm=None)
        self.llm = llm
        self.stats: Dict[str, int] = {
            "matches": 0, "with_picks": 0, "picks": 0,
            "llm_calls": 0, "llm_failures": 0, "no_llm": 0,
            # 门控统计：多少场/多少盘口被经济学算法拦下（不进入 LLM）
            "gated_out_matches": 0, "gated_out_markets": 0,
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
        """对一场的全部盘口做经济学计算（**不发任何 LLM 请求**）。

        ⚠️ **每个盘口只取「最新快照」一份**（本项目真实严重缺陷）：
        快照库是**不可变历史**，一场比赛同一盘口会累积几十份不同时间的
        快照（实测 `5726509` 共 216 条，其中 `OU(2.5)` 重复 **15** 次，
        赔率从 1.94 变化到 3.32）。早期实现直接遍历传入的全部快照，
        于是：
          * 同一盘口被计算/上报十几次（`/board` 的盘口列表出现重复项）；
          * 更严重的是**会挑到过期价格**：该场比分已是 1:3（4 球），
            `全场进球数>2.5` 早已结算，却因为取到了 1.8 小时前的
            赔率 3.32 而给出「买入 +7.57%」——**这是凭空造出的注单**，
            对应不上任何可成交的市场。

        因此这里先按 `(market, line)` 去重，只保留 `captured_at` 最新的
        一份，再送去做经济学计算。同时把快照年龄回传，供上层标注时效。
        """
        out: List[MarketComputation] = []
        trend_by_key = self._index_trend(trend)
        # `_small_model` 接受 Dict；这里统一成 dict 以避免只读映射的类型不匹配
        trend_dict: Optional[Dict[str, Any]] = dict(trend) if trend else None
        for snap in _latest_per_market(snapshots):
            if snap.state in (SnapshotState.SUSPENDED, SnapshotState.DELISTED):
                # 停盘/下架的价格不得参与决策（报告 §5.3）
                continue
            # **时效硬门禁**（用户报告问题 2）：赔率太旧就不是“数据缺失”，
            # 而是“不可交易”。实测该场比分已 1:3（4 球），却因为取到
            # 4.6 小时前的赔率 3.32 而给出「全场进球数>2.5 买入 +7.57%」
            # —— 那种注单对应不上任何可成交的市场。
            age_s = _snap_age_s(snap)
            if age_s is not None and age_s > self.config.max_quote_age_s:
                self.stats["stale_quotes"] = self.stats.get("stale_quotes", 0) + 1
                continue
            p_small, diag, _ = self.small._small_model(snap, trend_dict)
            odds = _safe_odds(snap.odds)
            edges = tuple(p * o - 1.0 for p, o in zip(p_small, odds))
            kellys = []
            for p, o, e in zip(p_small, odds, edges):
                k = 0.0
                if e > 0 and o > 1.0:
                    k = econ.fractional_kelly(p, o,
                                              lam=self.config.kelly_fraction)
                    k = max(0.0, min(k, self.config.max_stake_pct))
                kellys.append(k)

            # 走势查找键：必须用快照自带的 `(chpid, hv)`，
            # **不能用 `snap.market`**。
            #
            # 原因（本项目真实缺陷，已修复）：走势来自 WS 推送，其键是上游的
            # `(mid, chpid, hv)`；而 `snap.market` 是归一化后的代码（如
            # `AH(0.5)` / `HAD`）。两者**永不可能相等**，
            # 导致 `trend` 一直为空 → 整个走势维度（含调研 [C]/[D] 的
            # 资金流信号）形同死代码。
            meta = snap.metadata or {}
            ck = (str(meta.get("leyu_chpid") or ""),
                  str(meta.get("leyu_hv") or ""))
            trend_info: Dict[str, Any] = (trend_by_key.get(ck)
                                          or trend_by_key.get((ck[0], ""))
                                          or {})
            tick_age_s = _tick_age_s(trend_info.get("ts_ms"))
            # **阶段 1 门控**：市场质量前置条件 —— 该盘口值不值得问 LLM。
            # 注意这里用 `gate_market`（不含 edge 判定）：
            # 去水后的概率就是市场概率，小模型 edge 恒 ≤ 0，
            # 若在这里卡 edge 就永远不放行（详见 core/entry_gate.py）。
            gates = tuple(
                gate_market(
                    outcome=snap.outcomes[i],
                    odds=odds[i],
                    state=snap.state.value,
                    # 成交额在快照 metadata 里（乐鱼 `betAmount`），
                    # 不在 OddsSnapshot 顶层字段上。
                    bet_amount=_as_float(
                        (snap.metadata or {}).get("leyu_bet_amount")),
                    method_spread_pp=_as_float(diag.get("method_spread_pp")),
                    trend=str(trend_info.get("direction", "flat")),
                    trend_pct=_as_float(trend_info.get("delta_pct")) / 100.0,
                    tick_age_s=tick_age_s,
                    trend_ticks=_as_int(trend_info.get("n")),
                    # 水钱：去水分歧的阈值要随它放大（见 core/entry_gate）。
                    # 不传的话会退回绝对 3pp，重新造成大批判误杀。
                    margin=_as_float(diag.get("margin"), snap.margin),
                    # 走势修正后的 edge（信息流入但价格未走完时的真实优势）。
                    # 仅在**有走势数据**时作为阶段 1 的硬条件（见 core/entry_gate.py）。
                    pre_edge=edges[i],
                    config=self.entry_config,
                )
                for i in range(len(snap.outcomes))
            )
            out.append(MarketComputation(
                market=snap.market,
                outcomes=tuple(snap.outcomes),
                odds=odds,
                p_fair=tuple(p_small),
                edges=edges,
                kellys=tuple(kellys),
                gates=gates,
                margin=_as_float(diag.get("margin"), snap.margin),
                method=str(diag.get("method", "")),
                method_spread_pp=_as_float(diag.get("method_spread_pp")),
                # 原始线值：优先用落库时保存的 `leyu_hv`（如 `0/0.5`），
                # 它比代码里的中点（0.25）更准确，也是中文标签的依据。
                line=str((snap.metadata or {}).get("leyu_hv") or ""),
                state=snap.state.value,
                trend=str(trend_info.get("direction", "flat")),
                trend_pct=_as_float(trend_info.get("delta_pct")),
                trend_n=_as_int(trend_info.get("n")),
                # 队名：生成**乐鱼风格**中文选项名（如「曼联上半场-1」）
                # 必需 —— 让球盘不带队名就无法与乐鱼展示对齐。
                home=str(snap.home or ""),
                away=str(snap.away or ""),
            ))
        # 优势大的排前面：LLM 上下文有限，优先给它看有希望的
        out.sort(key=lambda c: c.best_edge, reverse=True)
        return out

    @staticmethod
    def _index_trend(
        trend: Optional[Mapping[str, Any]],
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """把 Hub 的走势结果索引成 `(chpid, hv) → 走势摘要`。

        为何用 `(chpid, hv)` 而非 `market`：走势推送的原始键就是
        上游的盘口 ID 与线值；`market` 是本项目归一化后的代码，
        两者没有直接映射关系（详见 `compute_markets` 内的说明）。
        同时保留以 `chpid` 单独为键的回退，兼容调用方只知盘口 ID 的情形。
        """
        if not trend:
            return {}
        markets = trend.get("markets") or []
        idx: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for m in markets:
            if not isinstance(m, Mapping):
                continue
            chpid = str(m.get("chpid") or "")
            hv = str(m.get("hv") or "")
            last = m.get("last") or {}
            if not isinstance(last, Mapping):
                continue
            info = {
                "direction": last.get("direction", "flat"),
                "delta_pct": last.get("delta_pct", 0.0),
                "n": m.get("n", 0),
                "up": m.get("up", 0),
                "down": m.get("down", 0),
                # 最近一次跳动的上游毫秒时间戳。
                # 门控用它算「重定价窗口」（[C] 调研结论）：
                # 刚跳完价时价格尚未稳定，入场应更谨慎。
                "ts_ms": last.get("ts", 0),
                # 走势跨度，用于把「变动次数」换算成活跃度
                "span_s": m.get("span_s", 0.0),
            }
            # 精确键优先：同一 chpid 下不同线值（如 OU 2.5 / 3.0）必须区分
            idx[(chpid, hv)] = info
            # 回退键：仅在无精确匹配时使用（`setdefault` 保证精确键优先）。
            # 注意：回退键只在**上游确实未给 hv**时才有意义；
            # 若上游给了 hv，`(chpid, "")` 不应被填充，
            # 否则会让「hv 不同」的盘口互相污染。
            if not hv:
                idx.setdefault((chpid, ""), info)
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
            "这是一场比赛中**已通过经济算法入场门槛**的盘口（市场公平概率已去水）。",
            "请给出**你自己的独立概率估计**，系统会用它重算优势。",
            "注意：这些盘口已经过确定性算法初筛，但仍需你独立判断；",
            "若你认为都不值得买，可返回空 markets 数组。",
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

        for c in list(comps)[:max(1, self.config.max_markets_per_prompt)]:
            trend_txt = ""
            if c.trend_n:
                trend_txt = "，走势 %s %.2f%%（%d 次变动）" % (
                    {"down": "降赔", "up": "升赔"}.get(c.trend, "未变"),
                    c.trend_pct, c.trend_n)
            # 告知该盘口的经济学门槛与余量：
            # 让 LLM 知道“这个盘口是过了什么关才被送来的”，
            # 而不是把所有盘口一视同仁地评估。
            gate_txt = ""
            bg = c.best_gate
            if bg is not None:
                gate_txt = "，经济门槛 %.2f%%（余量 %+.2f%%）" % (
                    bg.required_edge * 100, bg.margin * 100)
            # 用**人话标签**代替机器代码：
            #   既让 LLM 更好理解，也省 token（“上半场大1.5” vs “OU_1H(1.5) over”）
            lines.append("盘口 [%s] %s（水钱 %.2f%%%s%s）：" % (
                c.market, describe_market(c.market), c.margin * 100,
                trend_txt, gate_txt))
            order = sorted(range(len(c.outcomes)),
                           key=lambda i: c.p_fair[i], reverse=True)
            for i in order[:MAX_OUTCOMES_PER_MARKET]:
                # **必须同时给「结果键」与「中文标签」**：
                # 早期只给中文描述，而解析器按内部代码（`home`/`over`）取值，
                # 导致 LLM 返回中文键时**每一行都被静默丢弃**——
                # 实测这就是「几十场比赛零买入建议」的主因之一。
                lines.append(
                    "  - 键=%s | %s：赔率 %.3f，市场公平概率 %.4f"
                    % (c.outcomes[i],
                       format_market(c.market, c.outcomes[i], c.line,
                                     home=home, away=away),
                       c.odds[i], c.p_fair[i]))
        lines.append("")
        lines.append("降赔=资金流入=市场认为该结果概率上升。")
        lines.append("probabilities 的键必须逐字照抄上面的「键=」值。")
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

    def _required_edge_for(self, comp: MarketComputation, index: int) -> float:
        """取该盘口该结果在**阶段 2** 所需的最低 edge。

        优先用阶段 1 `gate_market()` 算出的动态门槛（含长赔/去水分歧/
        逆风/重定价等加价）；缺失时回退到全局 `min_edge`。

        为何不直接用 `self.config.min_edge`：调研结论 [B] 明确指出
        **固定阈值是错的** —— 同样 2% 在高赔/数据稀疏/刚跳价时
        更容易是噪声，必须按不确定性加价。
        """
        if 0 <= index < len(comp.gates):
            req = comp.gates[index].required_edge
            if req > 0:
                return req
        return self.config.min_edge

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
            # 必须给出该盘口的**全部**结果，否则无法计算优势。
            # 同时容忍 LLM 用中文标签作答（如「全场主队平手」）：
            # 早期只认内部代码，LLM 一旦用中文键就整行被丢，
            # 造成「有结果却零买入」的隐形故障。
            probs: List[float] = []
            ok = True
            for oc in comp.outcomes:
                v = _lookup_probability(raw, oc, mkt, comp.line,
                                        home=home, away=away)
                if v is None:
                    ok = False
                    break
                if not (0.0 <= v <= 1.0):
                    ok = False
                    break
                probs.append(v)
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
            #
            # ⚠️ 必须先**向市场收缩**再算 edge（本项目真实故障）：
            # 实测 LLM 在「只有赔率、无基本面」时倾向输出接近 50/50 的
            # 「均衡」估计（如市场 p=0.337 而 LLM 给 0.550，偏离 +21pp），
            # 若直接采用会凭空产生 +40% 的假 edge。
            # 收缩权重 = min(max_llm_weight, confidence)，与逐盘口引擎
            # `DecisionEngine._fuse` 保持一致（同一套概率融合语义）。
            w = min(self.config.max_llm_weight, max(0.0, conf))
            # **污染护栏**：LLM 对某结果的概率若与市场公平概率相差过大
            # （实测曾出现 p_llm=1.0 vs p_market=0.35），说明这不是
            # 独立判断而是泄露/幻觉。此时丢弃整个盘口，不产出任何建议，
            # 并在 stats 里计数以便监控。
            dev = max(abs(probs[i] - comp.p_fair[i])
                      for i in range(len(comp.outcomes)))
            if dev > self.config.max_prob_deviation:
                self.stats["contaminated"] = \
                    self.stats.get("contaminated", 0) + 1
                continue
            for i, oc in enumerate(comp.outcomes):
                p_llm_i = probs[i]
                p_fused = (1.0 - w) * comp.p_fair[i] + w * p_llm_i
                edge = p_fused * comp.odds[i] - 1.0
                # **阶段 2 门控**：用该盘口自己的动态门槛（而非全局常数）。
                # 门槛 = 基准 2% + 长赔/去水分歧/逆风/重定价/稀疏样本 加价，
                # 由 `gate_market()` 在阶段 1 算出并随快照传递（调研结论落地）。
                req = self._required_edge_for(comp, i)
                if edge < req:
                    continue
                kelly = econ.fractional_kelly(
                    p_fused, comp.odds[i], lam=self.config.kelly_fraction)
                kelly = max(0.0, min(kelly, self.config.max_stake_pct))
                picks.append({
                    "market": mkt,
                    "outcome": oc,
                    # **人话标签**：用户要求最终结果要说「曼联上半场-1」
                    # 「上半场进球数>1/1.5」，而不是 `OU_1H(1.5) over`。
                    # 传队名才能给出乐鱼风格的让球描述。
                    "pick_label": format_market(mkt, oc, comp.line,
                                                home=home, away=away),
                    "odds": round(comp.odds[i], 4),
                    "edge": round(edge, 6),
                    "edge_pct": round(edge * 100, 3),
                    # 回传门槛，便于人工复核「为何这注过/没过」
                    "required_edge": round(req, 6),
                    "required_edge_pct": round(req * 100, 3),
                    "kelly": round(kelly, 6),
                    "kelly_pct": round(kelly * 100, 4),
                    "confidence": round(conf, 4),
                    # 融合权重：让人能看出 LLM 到底被采纳了多少
                    "llm_weight": round(w, 4),
                    "reason": str(row.get("reason") or "")[:200],
                    # 可复核性：三套概率全部回传
                    "p_market": round(comp.p_fair[i], 6),
                    "p_llm": round(p_llm_i, 6),
                    "p_fused": round(p_fused, 6),
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
            # **如实区分原因**（用户报“为什么全是无机会”时最需要的信息）：
            # 早期一律写“停盘/下架或无快照”，但实测 2436 场全是
            # “赔率太旧被时效门禁拦下”（采集停摆 19.9 小时）——
            # 报错指向错误的方向，排查会完全跑偏。
            #
            # 用**本场自算**的数量，而不是引擎级累加计数器：
            # 后者跨场、跨线程共享（决策是并发的），拿它拼消息会偏大
            # 且存在竞争。
            latest = _latest_per_market(snaps)
            stale_n = sum(
                1 for s in latest
                if (_snap_age_s(s) or 0.0) > self.config.max_quote_age_s)
            if stale_n:
                res.error = (
                    "无可用盘口：盘口赔率已过期（超过 %.0f 秒）被全部拒用；"
                    "本场 %d/%d 个盘口过期 —— 通常是行情采集停摆所致"
                    % (self.config.max_quote_age_s, stale_n, len(latest)))
                res.llm_reason = res.error
            elif not latest:
                res.error = "无可用盘口：该场没有任何快照（尚未采集或已下架）"
            else:
                res.error = "无可用盘口：盘口全部停盘/下架"
            res.elapsed_ms = _elapsed_ms(t0)
            return res

        # 第 1.5 步：**入场门控**（用户要求的工作逻辑）。
        #
        # 只有通过经济门槛的盘口才交给 LLM。依据来自互联网调研：
        # 职业玩家不靠“看到机会就上”，而是先算清 EV 门槛与不确定性
        # （详见 core/entry_gate.py 的证据分级与来源）。
        #
        # 这同时解决了两个工程问题：
        #   1. LLM 上下文从 14~24 个盘口降到只剩“有可能”的几个；
        #   2. 全场都不通过时**直接回避**，省下一次 LLM 调用（与延迟）。
        candidates = [c for c in res.computations if c.gate_passed]
        res.gated_in = len(candidates)
        res.gated_out = len(res.computations) - len(candidates)
        if not candidates:
            res.decision = DECISION_AVOID
            self.stats["gated_out_matches"] += 1
            self.stats["gated_out_markets"] += res.gated_out
            res.llm_reason = (
                "经济算法未通过入场门槛（%d 个盘口全部不达标），"
                "未调用 LLM：%s"
                % (res.gated_out,
                   "、".join(_collect_rejects(res.computations)) or "edge 不足"))
            res.elapsed_ms = _elapsed_ms(t0)
            self.stats["matches"] += 1
            return res

        # 第 2/3 步：汇总给 LLM 做一次裁定（**只给通过门控的候选**）
        picks, conf, note, reason = self._ask_llm(
            candidates, res.home, res.away, res.league, context)
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

        res.elapsed_ms = _elapsed_ms(t0)
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
            # 入场门控参数（可审计：调研结论如何变成阈值）
            "entry_gate": {
                "base_min_edge": self.entry_config.base_min_edge,
                "longshot_odds": self.entry_config.longshot_odds,
                "longshot_penalty": self.entry_config.longshot_penalty,
                "repricing_quiet_s": self.entry_config.repricing_quiet_s,
                "repricing_penalty": self.entry_config.repricing_penalty,
                "adverse_trend_penalty": self.entry_config.adverse_trend_penalty,
                "thin_data_penalty": self.entry_config.thin_data_penalty,
                "min_trend_ticks": self.entry_config.min_trend_ticks,
                "hard_reject_trend_pct": self.entry_config.hard_reject_trend_pct,
            },
        }
