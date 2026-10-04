#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
入场门控（Entry Gate）—— 「什么时候该买入」的调研结论代码化。

═══════════════════════════════════════════════════════════════════════════
为什么需要这一层（用户要求的工作逻辑）
═══════════════════════════════════════════════════════════════════════════

    经济学算法（本模块）→ **通过** → 才调用 LLM 做最终买入裁定
                        → **不通过** → 直接回避，不浪费 LLM 调用

此前实现是「先算全部盘口 → 无差别交给 LLM 挑」：LLM 每场都要看 14~24 个
盘口，其中绝大多数在经济学上根本不成立。既慢（实测单场 23s、最坏 90s），
又让 LLM 承担了本可由确定性算法完成的粗筛工作。

═══════════════════════════════════════════════════════════════════════════
调研结论与证据分级（2026-10-04 互联网调研）
═══════════════════════════════════════════════════════════════════════════

[A] **EV 阈值分级**（行业实践，A 类：多来源一致）
    SharpAPI 文档给出明确分档，其默认门槛为 2%：

        EV%      质量        动作
        < 0%     负期望      永不投注
        0–2%     边际        仅高成交量时
        2–5%     良好        **标准门槛**
        5%+      优秀        高信心

    来源：docs.sharpapi.io/en/concepts/ev-calculation/

[B] **门槛必须随不确定性放大，而非固定值**（A 类：职业玩家共识）
    「同样的 +125，在好价格与不可投价格之间的差别只在赔率」；
    「你的概率估计才是脆弱输入，估计越弱 EV 结果越弱」；
    「固定阈值是错的」——长赔（longshot）下同样 2% 更容易是噪声。
    来源：coresportsbetting.com/how-to-set-edge-threshold-for-sports-betting/、
          betresearcher.com/guides/how-to-find-positive-ev-bets/

[C] **进球后的重定价窗口有确定的时间结构**（B 类：行业实测总结）
    进球 → 各盘口依次重定价，滞后顺序与时长（实测区间）：

        比赛结果盘 (1X2/HAD)   15–20s 重开     ← 基本无法利用
        亚盘 (AH)              20–30s 重开     ← 基本无法利用
        大小球 (OU)            +30–60s 滞后    ← 主要可利用层
        BTTS                   45–60s 滞后     ← 滞后最明显
        球员盘                 2–3 分钟        ← 最慢

    叠加**广播延迟**（有线 7–10s、流媒体 20–40s）→ 人工追价不可行；
    真正可用的场景是「进球不改变剩余进球预期，但市场按结果盘幅度调了
    大小球」这类**分离**情形（如第 85 分钟进球：结果概率剧变，
    但剩余时间太短，总进球概率几乎不变）。

    来源：betting-forum.com/threads/in-play-market-repricing-after-a-goal-the-90-second-window.47407/

[D] **市场对进球的反应存在系统性偏差**（A 类：学术）
    Angelini / De Angelis / Singleton 与 Croxson / Reade 的 in-play 研究：
    市场对**预期之中的进球反应不足**（underreact），
    对**意外进球反应过度**（overreact），且两者在统计与经济上均显著。
    含义：进球后的价格漂移方向可被识别，但不能靠"抢速度"，
    必须靠"判断这个进球是否意外"——即需要独立信息源。

    来源：ScienceDirect S0167268114000481（EJOR 310(3)）、
          centaur.reading.ac.uk/98329/、Croxson & Reade (2014) Economic Journal

[E] **CLV 是唯一领先指标**（A 类：职业玩家共识）
    「CLV 是长期盈利的最佳领先指标，因为它剥离短期方差，
    只检验你是否拿到了优于收盘价的价格」。
    但 CLV **不能事后重建**：必须在下注当时就记录
    （执行价、执行时间、参照盘口、收盘价、收盘时间），
    且「市场不匹配就没有可比性」。

    来源：sharksnip.com/blog/how-sharks-track-bets-clv-handbook

═══════════════════════════════════════════════════════════════════════════
本模块如何落地上述结论（**两阶段**，这是关键设计）
═══════════════════════════════════════════════════════════════════════════

⚠️ 一个必须说清的约束：**去水后的概率就是市场概率**，所以小模型的
edge 恒 ≤ 0（报告 §8.2）。因此「用小模型的 edge 当门槛」等于永不放行，
是把系统做死——**不能用它来门控**。

正确做法是把「经济学判断」拆成**前后两段**：

    阶段 1（问 LLM 之前）：`gate_market()` —— 市场质量前置条件
        盘口可交易 / 流动性 / 价格已稳定 / 非大幅逆风 / 去水可信
        → 决定「这个盘口值不值得去问 LLM」
        → 不通过则**不进 prompt**，省下 LLM 调用

    阶段 2（LLM 返回之后）：`required_edge()` —— 动态门槛
        LLM 给出**独立概率**后，系统重算 edge，
        再与本函数算出的「该盘口所需门槛」比较
        → 决定「这次买入裁定是否成立」

这样既满足用户要求的工作逻辑（经济学满足后才调 LLM），
又不会因为「小模型不可能有正 edge」而把链路堵死。

门槛不是一个常数，而是 **基准 + 一组可审计的加价项**：

    required_edge = base_min_edge                  # [A] 2% 标准门槛
                  + longshot_penalty               # [B] 长赔加价
                  + method_disagreement_penalty    # [B] 去水方法分歧
                  + adverse_trend_penalty          # [D] 走势逆风
                  + repricing_penalty              # [C] 刚进球/价格未稳
                  + thin_data_penalty              # [B] 走势样本太少

每一项都写进 `GateResult.checks`，可逐项复核「为什么拒绝」。

**诚实边界**：加价项的**数值**是量级示意（B/C 类证据 + 本项目经验），
不是实测标定值。但其**方向**明确且有据：不确定性越大 → 要求越高。
报告 §11.3 的同一纪律：方向可辩护，绝对值不应被当作精确估计。

依赖：仅标准库（AGENTS.md §1 核心层零依赖）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "EntryGateConfig",
    "GateResult",
    "gate_market",
    "evaluate_entry",
    "required_edge",
    "REJECT_EDGE",
    "REJECT_TREND",
    "REJECT_REPRICING",
    "REJECT_ILLIQUID",
    "REJECT_STATE",
    "REJECT_METHOD_SPREAD",
]

_EPS = 1e-12


def _num(value: object, default: float = 0.0) -> float:
    """容错浮点转换（本层会被上层用上游 JSON 直接调用，不能假设类型）。

    非有限值（NaN/Inf）一律回退默认，避免 NaN 比较静默把门槛算成 0。
    """
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


#: 拒绝原因码（供前端分组与统计，避免自由文本难以聚合）
REJECT_EDGE = "edge_below_threshold"
REJECT_TREND = "adverse_trend"
REJECT_REPRICING = "repricing_window"
REJECT_ILLIQUID = "insufficient_liquidity"
REJECT_STATE = "state_not_tradeable"
REJECT_METHOD_SPREAD = "devig_unreliable"


@dataclass(frozen=True)
class EntryGateConfig:
    """门控参数（全部具名，禁止魔法数字散落在表达式里）。"""

    #: [A] 基准门槛：行业标准档位下沿（SharpAPI 默认 2%）
    base_min_edge: float = 0.02
    #: [B] 长赔加价：赔率 ≥ `longshot_odds` 时额外要求的 edge
    longshot_odds: float = 4.0
    longshot_penalty: float = 0.01
    #: [B] 去水方法分歧 → 加价（分歧大 = 公平概率本身不确定）
    #: 每 1 个百分点分歧加价 `method_spread_penalty_per_pp`
    method_spread_penalty_per_pp: float = 0.005
    method_spread_cap: float = 0.02
    #: [D] 走势逆风加价：市场正在往我们反方向走 → 我们的估计可能已过期
    adverse_trend_threshold: float = 0.02
    adverse_trend_penalty: float = 0.01
    #: [C] 重定价窗口：最近一次跳动距今 < 该秒数 → 价格未稳，加价
    repricing_quiet_s: float = 45.0
    repricing_penalty: float = 0.015
    #: [B] 走势样本不足 → 加价（无法判断方向，等于没有走势信息）
    min_trend_ticks: int = 3
    thin_data_penalty: float = 0.005
    #: 硬性拒绝：走势逆风幅度超过该值时直接不参与
    #: （加价无法补偿"方向已明确不利"）
    hard_reject_trend_pct: float = 0.10
    #: 硬性拒绝：盘口成交额低于该值（乐鱼 `betAmount`）视为流动性不足
    min_bet_amount: float = 0.0
    #: 硬性拒绝：去水方法分歧超过该百分点时，市场公平概率本身不可信，
    #: 交给 LLM 也是问一个错误的问题（先修数据，不是先猜方向）。
    #:
    #: ⚠️ 这是**绝对下限**。真实故障：原实现只用这条 3pp 绝对阈值，
    #: 结果 69/71 场比赛都被 `devig_unreliable` 拒掉（用户看到的
    #: “几十场没有建议”的直接原因）。因为**分歧本质上随水钱放大**：
    #: 实测本仓库 1763 个盘口，`spread/margin` 中位 0.28、p90 0.69，
    #: 而水钱在不同玩法上差 3~5 倍（2 结果盘中位 1.75pp，
    #: 3 结果盘 4.51pp）。拿一个固定值去卡全部玩法必然误杀。
    max_method_spread_pp: float = 3.0
    #: 去水分歧的**相对允差**：允许分歧随水钱线性放大到 `ratio × margin`。
    #:
    #: 依据（本仓库数据实测，非拍脑袋）：
    #:   * spread/margin 中位 **0.28**、p75 0.45、p90 0.69、p95 0.92；
    #:   * 取 1.5 作为硬拒绝线 → 只拒 1% 的盘口（真正离群的），
    #:     而旧的绝对 3pp 要拒 35%。
    #: 即：分歧显著超出“该水钱水平下的正常放大”才判不可信。
    #: 取 1.5 而非 p95(0.92) 是为了留出安全余量，避免把正常的
    #: 高水钱盘口当成异常（宁可多问 LLM，也不要在数据层闲死）。
    method_spread_margin_ratio: float = 1.5
    #: 绝对上限（百分点）：即使水钱极大，分歧超过它仍直接拒。
    #: 防止极端脏数据（实测最大 24.6pp）漏网。
    method_spread_hard_cap_pp: float = 12.0
    #: 有走势数据时，是否要求「走势修正后的 edge」为正才放行。
    #:
    #: 这是**唯一能在 LLM 之前算出的真实经济信号**：
    #: 小模型在 `_small_model()` 里已根据赔率跳动（降赔=资金流入）
    #: 修正过公平概率，因此当市场正朝某结果定价而当前价格尚未跟足时，
    #: `p_adj·o − 1` 会为正——这正是调研 [C]/[D] 描述的「信息流入但价格未走完」。
    #:
    #: ⚠️ 仅当**存在走势数据**时才适用。无走势时 p_adj 就是去水概率，
    #: edge 恒 ≤ 0，若硬性要求为正则永远不放行（把系统做死）。
    require_pre_edge_when_trended: bool = True


@dataclass
class GateResult:
    """单个盘口/结果的门控结论（可审计）。

    `passed` 的语义随调用阶段而变：
      * `gate_market()`   —— 市场质量前置条件是否满足（是否值得问 LLM）
      * `evaluate_entry()` —— 含 edge 判定（LLM 的裁定是否成立）
    """

    passed: bool = False
    outcome: str = ""
    edge: Optional[float] = None
    required_edge: float = 0.0
    #: 未通过的原因码（可能多条）
    rejects: Tuple[str, ...] = ()
    #: 逐项明细，便于人工复核「为什么是这个门槛」
    checks: Dict[str, Any] = field(default_factory=dict)

    @property
    def margin(self) -> float:
        """edge 相对门槛的余量（正值表示通过）；无 edge 时返回 0。"""
        if self.edge is None:
            return 0.0
        return self.edge - self.required_edge

    def as_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "outcome": self.outcome,
            "edge": (None if self.edge is None else round(self.edge, 6)),
            "required_edge": round(self.required_edge, 6),
            "margin": round(self.margin, 6),
            "rejects": list(self.rejects),
            "checks": self.checks,
        }


def max_spread_pp(margin: float = 0.0,
                  config: Optional[EntryGateConfig] = None) -> float:
    """给定水钱水平下，去水分歧的**可接受上限**（百分点）。

    规则（依据见 `EntryGateConfig.method_spread_margin_ratio`）：

        limit = max(绝对下限 max_method_spread_pp,
                    ratio × margin_pp)
        limit = min(limit, method_spread_hard_cap_pp)

    为何不能只用固定值：实测本仓库 1763 个盘口，`spread/margin`
    中位 0.28、p90 0.69；而水钱在不同玩法上差 3~5 倍（2 结果盘中位
    1.75pp，3 结果盘 4.51pp）。固定 3pp 会把**正常的高水钱盘口**
    成批误杀 —— 真实故障：69/71 场因 `devig_unreliable` 被拒，
    用户看到的就是“几十场比赛零买入建议”。

    仍然保留绝对上限，是为了卡住真正的脏数据（实测最大 24.6pp）。
    """
    cfg = config or EntryGateConfig()
    margin_pp = max(0.0, _num(margin, 0.0) * 100.0)
    limit = max(cfg.max_method_spread_pp,
                cfg.method_spread_margin_ratio * margin_pp)
    return min(limit, cfg.method_spread_hard_cap_pp)


def required_edge(
    odds: float,
    *,
    method_spread_pp: float = 0.0,
    trend_pct: float = 0.0,
    trend_against: bool = False,
    tick_age_s: Optional[float] = None,
    trend_ticks: int = 0,
    config: Optional[EntryGateConfig] = None,
) -> Tuple[float, Dict[str, Any]]:
    """按 [A]+[B]+[C]+[D] 计算**该盘口**所需的 edge 门槛。

    门槛 = 基准 + Σ加价项。返回 `(门槛, 明细)`，明细可直接展示给用户，
    让「为什么这注要 3.2% 而不是 2%」有据可查。

    Args:
        odds: 十进制赔率（用于长赔加价）。
        method_spread_pp: 去水方法间分歧（百分点）。
        trend_pct: 走势净变动幅度（比例，0.03 = 3%）。
        trend_against: 走势是否与我们的方向相反。
        tick_age_s: 最近一次跳动距今秒数；None 表示无走势数据。
        trend_ticks: 走势样本数。
        config: 门控参数。
    """
    cfg = config or EntryGateConfig()
    base = cfg.base_min_edge
    parts: Dict[str, float] = {"base": round(base, 6)}
    odds_v = _num(odds, 0.0)
    spread_in = _num(method_spread_pp, 0.0)
    trend_v = _num(trend_pct, 0.0)

    # [B] 长赔加价：同样 2% 在长赔上更容易是噪声
    longshot = 0.0
    if odds_v >= cfg.longshot_odds:
        longshot = cfg.longshot_penalty
    parts["longshot"] = round(longshot, 6)

    # [B] 去水方法分歧：分歧越大，公平概率本身越不确定
    spread = max(0.0, spread_in)
    method_pen = min(spread * cfg.method_spread_penalty_per_pp,
                     cfg.method_spread_cap)
    parts["method_disagreement"] = round(method_pen, 6)

    # [D] 走势逆风：市场正朝我们反方向定价 → 我们的估计可能已过期
    adverse = 0.0
    if trend_against and abs(trend_v) >= cfg.adverse_trend_threshold:
        adverse = cfg.adverse_trend_penalty
    parts["adverse_trend"] = round(adverse, 6)

    # [C] 重定价窗口：刚发生过跳动（如进球）→ 价格尚未稳定
    repricing = 0.0
    if tick_age_s is not None and tick_age_s < cfg.repricing_quiet_s:
        repricing = cfg.repricing_penalty
    parts["repricing_window"] = round(repricing, 6)

    # [B] 走势样本不足：无法判断方向 ≈ 没有走势信息
    thin = 0.0
    if trend_ticks < cfg.min_trend_ticks:
        thin = cfg.thin_data_penalty
    parts["thin_data"] = round(thin, 6)

    total = base + longshot + method_pen + adverse + repricing + thin
    parts["total"] = round(total, 6)
    return total, parts


def evaluate_entry(
    *,
    outcome: str,
    odds: float,
    edge: float,
    state: str = "active",
    bet_amount: float = 0.0,
    method_spread_pp: float = 0.0,
    trend: str = "flat",
    trend_pct: float = 0.0,
    tick_age_s: Optional[float] = None,
    trend_ticks: int = 0,
    margin: float = 0.0,
    config: Optional[EntryGateConfig] = None,
) -> GateResult:
    """判断该结果是否**值得交给 LLM 做最终裁定**。

    这是「经济学算法满足后才调用 LLM」的那道闸门：
    未通过者不进入 LLM 上下文，直接回避。

    硬性拒绝（加价无法补偿）：
      * 盘口状态不可交易（停盘/下架）
      * 走势**明确且大幅**逆风（方向已不利，非不确定性）
      * 流动性低于下限（有下限时）
    其余情形通过「抬高门槛」表达不确定性，而不是一刀切拒绝。
    """
    cfg = config or EntryGateConfig()
    rejects: List[str] = []
    odds_v = _num(odds, 0.0)
    edge_v = _num(edge, 0.0)
    bet_v = _num(bet_amount, 0.0)
    spread_v = _num(method_spread_pp, 0.0)
    trend_v = _num(trend_pct, 0.0)

    # -- 硬性：状态可交易 -------------------------------------------------
    if str(state) not in ("active",):
        rejects.append(REJECT_STATE)

    # -- 硬性：流动性 -----------------------------------------------------
    if cfg.min_bet_amount > 0 and bet_v < cfg.min_bet_amount:
        rejects.append(REJECT_ILLIQUID)

    # -- 硬性：去水不可信（市场公平概率本身有问题）------------------------
    # 阈值随水钱放大：同一分歧在高水钱盘口是正常的，在低水钱盘口才是异常。
    if spread_v > max_spread_pp(margin, cfg):
        rejects.append(REJECT_METHOD_SPREAD)

    # -- 硬性：走势大幅逆风（方向不利，非"不确定"）------------------------
    against = _is_against(trend, trend_v)
    if against and abs(trend_v) >= cfg.hard_reject_trend_pct:
        rejects.append(REJECT_TREND)

    # -- 软性：抬高门槛 ---------------------------------------------------
    req, parts = required_edge(
        odds_v,
        method_spread_pp=spread_v,
        trend_pct=trend_v,
        trend_against=against,
        tick_age_s=tick_age_s,
        trend_ticks=trend_ticks,
        config=cfg,
    )
    if edge_v < req:
        rejects.append(REJECT_EDGE)

    checks: Dict[str, Any] = {
        "odds": round(odds_v, 4),
        "edge": round(edge_v, 6),
        "trend": trend,
        "trend_pct": round(trend_v, 6),
        "trend_against": against,
        "tick_age_s": (None if tick_age_s is None else round(_num(tick_age_s), 1)),
        "trend_ticks": trend_ticks,
        "method_spread_pp": round(spread_v, 4),
        "method_spread_limit_pp": round(max_spread_pp(margin, cfg), 4),
        "threshold_parts": parts,
    }
    return GateResult(
        passed=not rejects,
        outcome=str(outcome),
        edge=edge_v,
        required_edge=req,
        rejects=tuple(rejects),
        checks=checks,
    )


def gate_market(
    *,
    outcome: str = "",
    odds: float = 0.0,
    state: str = "active",
    bet_amount: float = 0.0,
    method_spread_pp: float = 0.0,
    trend: str = "flat",
    trend_pct: float = 0.0,
    tick_age_s: Optional[float] = None,
    trend_ticks: int = 0,
    pre_edge: Optional[float] = None,
    margin: float = 0.0,
    config: Optional[EntryGateConfig] = None,
) -> GateResult:
    """**阶段 1**：经济前置条件 —— 该盘口值不值得去问 LLM。

    两类条件：

    1. **市场质量**（始终检查）
       可交易 / 流动性 / 去水可信 / 走势非大幅不利

    2. **走势信息优势**（仅在存在走势数据时检查）
       调研 [C]/[D] 的核心可操作信号：赔率跳动代表资金流入。
       小模型已在 `_small_model()` 中据此修正公平概率，
       因此当市场正朝某结果定价、而当前价格尚未走完时，
       `pre_edge = p_adj·o − 1 > 0`——这是**LLM 之前唯一真实的经济信号**。

    ⚠️ **刻意不把 `pre_edge` 作为无条件门槛**：
    无走势数据时 `p_adj` 就是去水概率，edge 恒 ≤ 0（报告 §8.2），
    硬性要求为正等于永不放行，会把整条决策链路做死。

    真正的**最终** edge 门槛在 LLM 给出独立概率之后，
    由 `required_edge()` + `evaluate_entry()` 执行（阶段 2）。
    """
    cfg = config or EntryGateConfig()
    rejects: List[str] = []
    odds_v = _num(odds, 0.0)
    bet_v = _num(bet_amount, 0.0)
    spread_v = _num(method_spread_pp, 0.0)
    trend_v = _num(trend_pct, 0.0)
    pre_edge_v = None if pre_edge is None else _num(pre_edge, 0.0)

    if str(state) not in ("active",):
        rejects.append(REJECT_STATE)
    if cfg.min_bet_amount > 0 and bet_v < cfg.min_bet_amount:
        rejects.append(REJECT_ILLIQUID)
    # 去水分歧：阈值随水钱放大（理由见 `max_spread_pp` 与配置注释）。
    # 早期只用固定 3pp，导致 69/71 场被 `devig_unreliable` 拒掉。
    if spread_v > max_spread_pp(margin, cfg):
        rejects.append(REJECT_METHOD_SPREAD)
    against = _is_against(trend, trend_v)
    if against and abs(trend_v) >= cfg.hard_reject_trend_pct:
        rejects.append(REJECT_TREND)

    # 走势信息优势：**仅当有走势数据时**才是硬条件
    has_trend = trend_ticks > 0
    if (cfg.require_pre_edge_when_trended and has_trend
            and pre_edge_v is not None and pre_edge_v <= 0.0):
        rejects.append(REJECT_EDGE)

    # 门槛仍然算出并展示（阶段 2 会用它判定 LLM 的 edge），
    # 但**不参与**这里的通过与否。
    req, parts = required_edge(
        odds_v,
        method_spread_pp=spread_v,
        trend_pct=trend_v,
        trend_against=against,
        tick_age_s=tick_age_s,
        trend_ticks=trend_ticks,
        config=cfg,
    )
    return GateResult(
        passed=not rejects,
        outcome=str(outcome),
        edge=None,                      # 阶段 1 不做最终 edge 判定
        required_edge=req,
        rejects=tuple(rejects),
        checks={
            "stage": "market_quality",
            "odds": round(odds_v, 4),
            "trend": trend,
            "trend_pct": round(trend_v, 6),
            "trend_against": against,
            "has_trend": has_trend,
            "pre_edge": (None if pre_edge_v is None
                         else round(pre_edge_v, 6)),
            "tick_age_s": (None if tick_age_s is None
                           else round(_num(tick_age_s), 1)),
            "trend_ticks": trend_ticks,
            "method_spread_pp": round(spread_v, 4),
            "method_spread_limit_pp": round(max_spread_pp(margin, cfg), 4),
            "margin": round(_num(margin, 0.0), 6),
            "threshold_parts": parts,
        },
    )


def _is_against(trend: str, trend_pct: float) -> bool:
    """走势是否与「我们看好的方向」相反。

    走势语义（见 `collector.leyu_realtime`）：赔率**下降** = 资金流入该结果
    = 市场认为其概率上升。因此对本结果的 edge 判断而言，
    `down` 是顺风、`up` 是逆风。
    """
    return str(trend).lower() == "up" and abs(_num(trend_pct, 0.0)) > _EPS
