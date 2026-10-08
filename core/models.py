#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核心数据模型

本模块定义贯穿 L1→L6 全链路的不可变数据结构。
设计约束：
  - 仅使用标准库（AGENTS.md §1：宿主机依赖不全，核心算法必须零依赖可测）
  - 全部为 frozen dataclass，保证快照语义不可变（报告 §12.1「不可变快照」）
  - 金额/概率一律用 float，但概率在构造时做区间校验

对应报告章节：
  - OddsSnapshot / SnapshotState  → §4.2 赔率快照状态语义、§5.3 停盘处理
  - FairProbabilities             → §8.1 去水
  - ValuationResult / EdgeResult  → §8.2 优势估计
  - SizingResult                  → §8.5 多元 Kelly
  - CalibrationReport             → §8.8 校准闭环
  - MicrostructureSignal          → §5.1 微结构信号
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

__all__ = [
    "SnapshotState",
    "DevigMethod",
    "OddsSnapshot",
    "FairProbabilities",
    "EdgeResult",
    "ExecutionFilter",
    "SizingResult",
    "MicrostructureSignal",
    "CalibrationReport",
    "utcnow",
]


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #

class SnapshotState(str, Enum):
    """赔率快照状态。

    报告 §5.3：停盘（suspend）期间读到的价格是「冻结值」，不得进入
    微结构信号与 edge 计算；报告 §7.1：陈旧价格是「未定价」与「报价撤回」
    的混合，同样不可用于 edge。
    """

    ACTIVE = "active"          # 有效，可用于计算
    SUSPENDED = "suspended"    # 停盘：关键事件挂起
    STALE = "stale"            # 陈旧：超过刷新窗口未见更新
    DELISTED = "delisted"      # 已下架：赛事结束或撤盘

    @property
    def usable_for_signal(self) -> bool:
        """是否可用于信号计算（微结构 / edge）。"""
        return self is SnapshotState.ACTIVE

    @property
    def usable_for_quote(self) -> bool:
        """是否可用于「观察性」报价展示（停盘/陈旧仍可展示，但需标注）。"""
        return self is not SnapshotState.DELISTED


class DevigMethod(str, Enum):
    """去水方法（报告 §8.1）。"""

    PROPORTIONAL = "proportional"   # 比例归一化
    ADDITIVE = "additive"           # 加法法
    POWER = "power"                 # 幂法
    ODDS_RATIO = "odds_ratio"       # 赔率比法（log-odds 平移）
    SHIN = "shin"                   # Shin 模型（内生 FL bias 修正）
    AUTO = "auto"                   # 自动选择（以 Shin 为基准，失败则回退）


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def utcnow() -> datetime:
    """带时区的当前时间（避免 naive datetime 比较错误）。"""
    return datetime.now(timezone.utc)


def _as_finite_float(value: Any, name: str) -> float:
    """把任意输入安全地转成有限 float。

    覆盖 Decimal、numpy 标量、字符串数字等非原生类型；
    转换失败一律抛 ValueError，保证调用方可预测。
    """
    if isinstance(value, bool):
        raise TypeError("%s 不能是布尔值" % name)
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("%s 无法转换为数值: %r" % (name, value)) from exc
    if math.isnan(out) or math.isinf(out):
        raise ValueError("%s 不能为 NaN/Inf" % name)
    return out


def _check_prob(p: float, name: str) -> float:
    """校验概率在 [0, 1] 内。"""
    out = _as_finite_float(p, name)
    if out < 0.0 or out > 1.0:
        raise ValueError("%s 必须在 [0,1] 内，实际为 %r" % (name, p))
    return out


def _check_odds(o: float, name: str) -> float:
    """校验十进制赔率 > 1.0（含本金）。"""
    out = _as_finite_float(o, name)
    if out <= 1.0:
        raise ValueError(
            "%s 必须 > 1.0（十进制含本金赔率），实际为 %r" % (name, o)
        )
    return out


# --------------------------------------------------------------------------- #
# L1 快照
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class OddsSnapshot:
    """单个市场在某一时刻的赔率快照（不可变）。

    Attributes:
        match_id: 赛事唯一标识。
        league: 联赛简称（如 "日职"）。
        home: 主队名。
        away: 客队名。
        market: 市场代码（"1X2" / "AH" / "OU" 等）。
        outcomes: 结果名列表，如 ("home", "draw", "away")。
        odds: 与 outcomes 一一对应的十进制赔率。
        state: 快照状态，见 SnapshotState。
        captured_at: 采集时间（UTC）。
        source: 数据来源标识。
        metadata: 附加原始信息（不可变映射语义，请勿依赖其可哈希性）。
    """

    match_id: str
    league: str
    home: str
    away: str
    market: str
    outcomes: Tuple[str, ...]
    odds: Tuple[float, ...]
    state: SnapshotState = SnapshotState.ACTIVE
    captured_at: datetime = field(default_factory=utcnow)
    source: str = "unknown"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.match_id:
            raise ValueError("match_id 不能为空")
        if len(self.outcomes) != len(self.odds):
            raise ValueError(
                "outcomes 与 odds 长度不一致: %d vs %d"
                % (len(self.outcomes), len(self.odds))
            )
        if len(self.outcomes) < 2:
            raise ValueError("市场至少需要 2 个结果，实际 %d" % len(self.outcomes))
        if len(set(self.outcomes)) != len(self.outcomes):
            raise ValueError("outcomes 存在重复项: %r" % (self.outcomes,))

        # 关键：上游 JSON 把赔率存为字符串（如 "2.13"）。frozen dataclass
        # 不能直接赋值，故用 object.__setattr__ 把校验后的 float 写回；
        # 否则后续 booksum/raw_implied 会因 str 参与除法而 TypeError。
        normalized = tuple(
            _check_odds(o, "odds[%d]" % i) for i, o in enumerate(self.odds)
        )
        if normalized != self.odds:
            object.__setattr__(self, "odds", normalized)

    # -- 派生量 -------------------------------------------------------------

    @property
    def raw_implied(self) -> Tuple[float, ...]:
        """原始隐含概率 q_i = 1/o_i（未去水）。"""
        return tuple(1.0 / o for o in self.odds)

    @property
    def booksum(self) -> float:
        """B = Σ(1/o_i)。"""
        return math.fsum(self.raw_implied)

    @property
    def margin(self) -> float:
        """水钱 m = B − 1。

        注意：m 可能为负（数据异常或真实套利），调用方需自行处理。
        """
        return self.booksum - 1.0

    @property
    def n_outcomes(self) -> int:
        return len(self.outcomes)

    def is_usable(self) -> bool:
        """是否可用于信号计算（报告 §5.3/§7.1）。"""
        return self.state.usable_for_signal

    def as_dict(self) -> Dict[str, Any]:
        """序列化为可 JSON 化的字典。

        ⚠️ **必须包含 metadata**：上游溯源信息（盘口线 `leyu_hv`、数据源 `leyu_cds`、
        盘口名等）都存那里。早期实现漏了它，导致落库后再读回时盘口线丢失，
        中文标签无法还原（如“上半场大1.5”只能退回中点 0.75）。
        """
        return {
            "match_id": self.match_id,
            "league": self.league,
            "home": self.home,
            "away": self.away,
            "market": self.market,
            "outcomes": list(self.outcomes),
            "odds": list(self.odds),
            "state": self.state.value,
            "captured_at": self.captured_at.isoformat(),
            "source": self.source,
            "booksum": round(self.booksum, 8),
            "margin": round(self.margin, 8),
            "metadata": dict(self.metadata or {}),
        }


# --------------------------------------------------------------------------- #
# L2 去水结果
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FairProbabilities:
    """去水后的公平概率及其诊断信息（报告 §8.1）。

    Attributes:
        probabilities: 去水后概率，与 outcomes 同序。
        method: 实际使用的方法。
        margin: 该市场的 booksum 水钱。
        shin_z: Shin 模型估计的内幕交易者比例 z（非 Shin 方法为 None）。
        method_spread_pp: 各方法之间的最大 L1 偏差（百分点）。
            报告 §8.1：实测可达 8.0 pp，足以翻转 edge 符号。
        spread_warning: 偏差是否超过告警阈值。
        per_method: 各方法各自的概率，便于 UI 展示对比。
        notes: 计算过程中的告警信息（如加法法产生负概率）。
    """

    probabilities: Tuple[float, ...]
    method: DevigMethod
    margin: float
    outcomes: Tuple[str, ...]
    shin_z: Optional[float] = None
    method_spread_pp: float = 0.0
    spread_warning: bool = False
    per_method: Mapping[str, Tuple[float, ...]] = field(default_factory=dict)
    notes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for i, p in enumerate(self.probabilities):
            _check_prob(p, "probabilities[%d]" % i)

    @property
    def sums_to_one(self) -> bool:
        return abs(math.fsum(self.probabilities) - 1.0) < 1e-6

    def as_dict(self) -> Dict[str, Any]:
        return {
            "probabilities": [round(p, 8) for p in self.probabilities],
            "outcomes": list(self.outcomes),
            "method": self.method.value,
            "margin": round(self.margin, 8),
            "shin_z": None if self.shin_z is None else round(self.shin_z, 8),
            "method_spread_pp": round(self.method_spread_pp, 4),
            "spread_warning": self.spread_warning,
            "per_method": {
                k: [round(x, 8) for x in v] for k, v in self.per_method.items()
            },
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# L4 优势与仓位
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ExecutionFilter:
    """执行可行性（报告 §8.3）。

    报告 §8.3 的核心：q_fill 对 EV 与注额 s 均为**负偏导**——
    优势越大越易被拒单，注额越大越易被降限额。
    """

    q_fill: float                 # 成交概率估计 ∈ [0,1]
    stake: float                  # 计划注额
    cost_exec: float = 0.0        # 单次执行成本
    notes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_prob(self.q_fill, "q_fill")
        if self.stake < 0:
            raise ValueError("stake 不能为负，实际 %r" % self.stake)

    @property
    def effective_multiplier(self) -> float:
        """有效期望的乘数项 q_fill。"""
        return self.q_fill


@dataclass(frozen=True)
class EdgeResult:
    """优势估计结果（报告 §8.2–§8.4）。"""

    outcomes: Tuple[str, ...]
    odds: Tuple[float, ...]
    p_model: Tuple[float, ...]        # 收缩后的模型概率
    p_model_raw: Tuple[float, ...]    # 收缩前的原始模型概率
    p_fair: Tuple[float, ...]         # 去水公平概率
    edge: Tuple[float, ...]           # edge_i = p_i * o_i − 1
    ev: Tuple[float, ...]             # 名义期望（等价于 edge，保留以便阅读）
    ev_effective: Tuple[float, ...]   # 含 q_fill 与执行成本的有效期望
    shrink_weight: float              # 收缩权重 w
    margin: float
    spread_warning: bool = False
    notes: Tuple[str, ...] = ()

    def best(self) -> Optional[Tuple[str, float]]:
        """返回 edge 最大的 (outcome, edge)；空则 None。"""
        if not self.edge:
            return None
        idx = max(range(len(self.edge)), key=lambda i: self.edge[i])
        return self.outcomes[idx], self.edge[idx]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "outcomes": list(self.outcomes),
            "odds": list(self.odds),
            "p_model": [round(p, 8) for p in self.p_model],
            "p_model_raw": [round(p, 8) for p in self.p_model_raw],
            "p_fair": [round(p, 8) for p in self.p_fair],
            "edge": [round(e, 8) for e in self.edge],
            "ev": [round(e, 8) for e in self.ev],
            "ev_effective": [round(e, 8) for e in self.ev_effective],
            "shrink_weight": round(self.shrink_weight, 6),
            "margin": round(self.margin, 8),
            "spread_warning": self.spread_warning,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class SizingResult:
    """仓位建议（报告 §8.5）。**仅用于研究，不构成投注建议。**"""

    fractions: Tuple[float, ...]      # 各注占银行比例
    outcomes: Tuple[str, ...]
    lam: float                        # 分数 Kelly 系数 λ
    used_covariance: bool             # 是否使用了协方差矩阵（多元）
    correlation_penalty: float        # naive 高估倍数 1/(1−ρ) 的估计
    total_exposure: float             # 总敞口 Σf
    notes: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "fractions": [round(f, 8) for f in self.fractions],
            "outcomes": list(self.outcomes),
            "lam": self.lam,
            "used_covariance": self.used_covariance,
            "correlation_penalty": round(self.correlation_penalty, 6),
            "total_exposure": round(self.total_exposure, 8),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# L1′ 微结构
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MicrostructureSignal:
    """微结构信号（报告 §5.1）。

    报告 §5.1 指出这是唯一不含「报价撤回」污染的信息维度，
    但同时也警告：在庄家单方面报价的市场中，跳动很大一部分是风控行为。
    """

    match_id: str
    outcome: str
    tick_count: int                     # 样本窗口内跳动次数
    tick_frequency: float               # 跳动频率（次/秒）
    recovery_seconds: Optional[float]   # 平均恢复时间（跳价后回到新稳态）
    drift_rate: float                   # 漂移速率（赔率/秒，带符号）
    suspension_seconds: float           # 累计停盘时长
    n_suspended: int                    # 停盘次数
    window_seconds: float
    usable: bool = True                 # 是否纳入信号统计
    notes: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "match_id": self.match_id,
            "outcome": self.outcome,
            "tick_count": self.tick_count,
            "tick_frequency": round(self.tick_frequency, 6),
            "recovery_seconds": (
                None if self.recovery_seconds is None
                else round(self.recovery_seconds, 6)
            ),
            "drift_rate": round(self.drift_rate, 8),
            "suspension_seconds": round(self.suspension_seconds, 4),
            "n_suspended": self.n_suspended,
            "window_seconds": round(self.window_seconds, 4),
            "usable": self.usable,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# L6 校准
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CalibrationReport:
    """校准报告（报告 §8.8）。"""

    n: int
    brier: Optional[float]
    ece: Optional[float]
    log_growth: Optional[float]
    max_drawdown: Optional[float]
    clv_mean: Optional[float]
    bins: Tuple[Mapping[str, Any], ...] = ()
    notes: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        def r(x: Optional[float]) -> Optional[float]:
            return None if x is None else round(x, 8)

        return {
            "n": self.n,
            "brier": r(self.brier),
            "ece": r(self.ece),
            "log_growth": r(self.log_growth),
            "max_drawdown": r(self.max_drawdown),
            "clv_mean": r(self.clv_mean),
            "bins": [dict(b) for b in self.bins],
            "notes": list(self.notes),
        }
