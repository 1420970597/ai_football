#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投注结算（纯标准库）。

## 为什么单独放在 `core/`

结算规则是**可独立验证的数学**，不应与文件 IO / LLM / HTTP 混在一起。
放在 `core/` 也让它可以被离线回测脚本直接复用（本项目已有
`reports/leyu-kaiyun-odds-bot-feasibility/` 这类研究目录）。

## 结算规则（对齐亚洲盘口行业惯例）

| 盘口 | 判定 | 可能结果 |
| --- | --- | --- |
| `HAD` 独赢 | 主/平/客 | 赢 / 输 |
| `OU` 大小 | 总进球 vs 线值 | 赢 / 输 / **走水** |
| `AH` 让球 | 净胜球 + 让球线 | 赢 / 输 / **走水** / **赢半** / **输半** |

**复合盘**（`0/0.5`、`2/2.5`）必须**拆成两个半注**分别判定再取平均。
这是本项目真实数据里最常见的线值写法（乐鱼 `hv` 字段），
若只取中点近似（0.25）会算错一半的注单。

`_1H` 后缀表示上半场盘口，必须用**半场比分**结算；拿不到半场比分时
返回 `void` 而**不是**猜测 —— 诚实性优先（报告 §5.3 同款原则：
不确定的数据不得进入收益统计）。

## pnl 语义

`pnl` 是**每 1 单位本金**的净收益：

* 赢 → `odds - 1`（如 2.0 赔率赢 → +1.0）
* 输 → `-1.0`
* 走水 → `0.0`
* 赢半 → `(odds - 1) / 2`（另一半退回）
* 输半 → `-0.5`
"""

from __future__ import annotations

import math

from typing import Any, List, Optional, Sequence, Tuple

from .market_labels import parse_market_code
from .calibration import clv as _calibration_clv

__all__ = [
    "SETTLE_PENDING",
    "SETTLE_WON",
    "SETTLE_LOST",
    "SETTLE_PUSH",
    "SETTLE_HALF_WON",
    "SETTLE_HALF_LOST",
    "SETTLE_VOID",
    "TERMINAL_STATUSES",
    "split_line",
    "settle_pick",
    "pnl_for",
    "implied_probability",
    "clv",
    "summarise",
]

SETTLE_PENDING = "pending"
SETTLE_WON = "won"
SETTLE_LOST = "lost"
SETTLE_PUSH = "push"            # 走水：退回本金
SETTLE_HALF_WON = "half_won"    # 赢半
SETTLE_HALF_LOST = "half_lost"  # 输半
#: 无法结算（缺半场比分、盘口无法解析等）。**不是**「输」——
#: 混入统计会系统性低估收益，必须单独剔除。
SETTLE_VOID = "void"

#: 已终结（不再等待结算）的状态
TERMINAL_STATUSES = frozenset({
    SETTLE_WON, SETTLE_LOST, SETTLE_PUSH,
    SETTLE_HALF_WON, SETTLE_HALF_LOST, SETTLE_VOID,
})

#: 有实际盈亏意义的状态（`void` 被排除在命中率/ROI 之外）
GRADED_STATUSES = frozenset({
    SETTLE_WON, SETTLE_LOST, SETTLE_PUSH,
    SETTLE_HALF_WON, SETTLE_HALF_LOST,
})

#: 结果名归一化（兼容中文/乐鱼原始 ot 值/大小写）
_HOME = frozenset({"home", "1", "主", "主胜", "h"})
_DRAW = frozenset({"draw", "x", "平", "平局", "d"})
_AWAY = frozenset({"away", "2", "客", "客胜", "a"})
_OVER = frozenset({"over", "大", "o"})
_UNDER = frozenset({"under", "小", "u"})

Score = Tuple[int, int]


def split_line(line: object) -> List[float]:
    """把线值拆成若干**半注**线值。

    `0/0.5` → `[0.0, 0.5]`（两个半注）
    `2.5`   → `[2.5]`（整注）

    无法解析时返回空列表（调用方据此判 void）。
    """
    if isinstance(line, bool):
        return []
    s = str(line if line is not None else "").strip().replace("＋", "+")
    try:
        values = [float(x.strip()) for x in s.split("/")]
    except (ValueError, OverflowError):
        return []
    if not values or len(values) > 2 or any(not math.isfinite(x) or abs(x) > 100 for x in values):
        return []
    if len(values) == 2:
        if any(abs(x * 2 - round(x * 2)) > 1e-8 for x in values):
            return []
        return values if abs(abs(values[0] - values[1]) - .5) < 1e-8 else []
    value = values[0]
    if abs(value * 4 - round(value * 4)) > 1e-8:
        return []
    if abs(value * 2 - round(value * 2)) > 1e-8:
        low = math.floor(value * 2) / 2
        return [low, low + .5]
    return [value]


def _norm_outcome(outcome: object) -> str:
    o = str(outcome if outcome is not None else "").strip().lower()
    if o in _HOME:
        return "home"
    if o in _DRAW:
        return "draw"
    if o in _AWAY:
        return "away"
    if o in _OVER:
        return "over"
    if o in _UNDER:
        return "under"
    return ""


def _score_of(score: object) -> Optional[Score]:
    """把任意比分表示转成 `(主, 客)`；不可用时返回 None。"""
    if isinstance(score, dict):
        if "home" not in score or "away" not in score:
            return None
        parts = [score["home"], score["away"]]
    elif isinstance(score, (tuple, list)) and len(score) == 2:
        parts = list(score)
    else:
        return None
    out = []
    for part in parts:
        if isinstance(part, bool) or part is None:
            return None
        try:
            value = float(part)
        except (ValueError, TypeError, OverflowError):
            return None
        if not math.isfinite(value) or not 0 <= value <= 100 or value != int(value):
            return None
        out.append(int(value))
    return out[0], out[1]


def _grade_margin(margin: float) -> str:
    """让球/大小球的单注判定：`margin > 0` 赢、`< 0` 输、`== 0` 走水。"""
    if margin > 1e-9:
        return SETTLE_WON
    if margin < -1e-9:
        return SETTLE_LOST
    return SETTLE_PUSH


def pnl_for(status: str, odds: float) -> float:
    """把判定结果换算成**每单位本金**的净收益。"""
    if implied_probability(odds) is None:
        return 0.0
    o = float(odds)
    if status == SETTLE_WON:
        return o - 1.0
    if status == SETTLE_LOST:
        return -1.0
    if status == SETTLE_HALF_WON:
        return (o - 1.0) / 2.0
    if status == SETTLE_HALF_LOST:
        return -0.5
    if status == SETTLE_PUSH:
        return 0.0
    return 0.0


def _aggregate(parts: Sequence[str]) -> str:
    """把多个半注的判定合并成一个最终状态。"""
    if not parts:
        return SETTLE_VOID
    wins = sum(1 for p in parts if p == SETTLE_WON)
    losses = sum(1 for p in parts if p == SETTLE_LOST)
    pushes = sum(1 for p in parts if p == SETTLE_PUSH)
    n = len(parts)
    if n == 1:
        return parts[0]
    if n == 2:
        # 复合盘只有两个半注，语义固定
        if wins == 1 and losses == 1:
            # 半赢半输（如 0/0.5 盘打平：0 走水？不 —— 0 输、0.5 输）
            # 实际只有「赢半」（赢+走水）与「输半」（输+走水）
            return SETTLE_VOID
        if wins == 1 and pushes == 1:
            return SETTLE_HALF_WON
        if losses == 1 and pushes == 1:
            return SETTLE_HALF_LOST
        if pushes == 2:
            return SETTLE_PUSH
        if wins == 2:
            return SETTLE_WON
        if losses == 2:
            return SETTLE_LOST
    # 超过两个半注（异常线值）：保守判 void，不猜测
    return SETTLE_VOID


def settle_pick(market: object, outcome: object,
                line: object = "",
                ft_score: object = None,
                ht_score: object = None) -> Tuple[str, str]:
    """结算一注。

    Args:
        market: 盘口代码（如 `OU_1H(1.5)`、`AH(-0.5)`、`HAD`）。
        outcome: 结果名（`home`/`draw`/`away`/`over`/`under`，兼容中文）。
        line: **原始线值**（如 `0/0.5`）；为空时回退用代码里的线值。
        ft_score: 全场比分 `(主, 客)`。
        ht_score: 半场比分 `(主, 客)`；上半场盘口必需。

    Returns:
        `(状态, 说明)`。状态见模块常量；说明用于人工复核
        （如 `"上半场盘口缺半场比分"`）。
    """
    parsed = parse_market_code(market)
    if parsed is None:
        return SETTLE_VOID, "无法解析盘口代码"
    fam, is_half, code_line = parsed
    oc = _norm_outcome(outcome)
    if not oc:
        return SETTLE_VOID, "无法解析结果名"

    score = _score_of(ht_score if is_half else ft_score)
    if score is None:
        return SETTLE_VOID, ("缺少半场比分" if is_half else "缺少全场比分")
    home, away = score

    if fam == "HAD":
        if oc not in ("home", "draw", "away"):
            return SETTLE_VOID, "独赢盘结果名不匹配"
        actual = "home" if home > away else ("away" if away > home else "draw")
        return (SETTLE_WON if oc == actual else SETTLE_LOST), ""

    ln = str(line if line is not None else "").strip() or code_line
    halves = split_line(ln)
    if not halves:
        return SETTLE_VOID, "无法解析线值"

    parts: List[str] = []
    if fam == "OU":
        if oc not in ("over", "under"):
            return SETTLE_VOID, "大小球结果名不匹配"
        total = home + away
        for h in halves:
            # 大：总进球 - 线；小：线 - 总进球
            margin = (total - h) if oc == "over" else (h - total)
            parts.append(_grade_margin(margin))
        return _aggregate(parts), ""

    if fam == "AH":
        if oc not in ("home", "away"):
            return SETTLE_VOID, "让球盘结果名不匹配"
        diff = home - away
        for h in halves:
            # 让球线以**主队**为基准：负=主队让球，正=主队受让
            margin = diff + h if oc == "home" else -diff - h
            parts.append(_grade_margin(margin))
        return _aggregate(parts), ""

    return SETTLE_VOID, "未知盘口族"


def implied_probability(odds: float) -> Optional[float]:
    """赔率 → 隐含概率（含水位）。赔率非法时返回 None。"""
    try:
        o = float(odds)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(o) or o <= 1.0:
        return None
    return 1.0 / o


def clv(entry_odds: float, closing_odds: float) -> Optional[float]:
    """闭线价值（CLV）：以买入价相对收盘价的优势。

    对**买入**（back）注：`entry / closing - 1`。

    * 正 → 我们买得比收盘价便宜（赚到了价格，长期正期望的信号）
    * 负 → 我们买贵了

    为何 CLV 比单场输赢更重要：单场结果方差极大，几十注完全看不出
    模型好坏；而 CLV 衡量「是否持续拿到好价格」，样本需求低得多
    （这是博彩量化领域的共识做法）。
    """
    try:
        return _calibration_clv(entry_odds, closing_odds)
    except (TypeError, ValueError, OverflowError):
        return None


def summarise(rows: Sequence[Any]) -> dict:
    """对已结算台账做本地统计（命中率 / ROI / CLV）。

    只统计 `GRADED_STATUSES` 的行；`pending` 与 `void` 单独计数，
    **不混入**收益指标（否则会系统性低估 ROI）。

    ⚠️ **本金只算真正下注的行**（`is_pick=True`）：台账同时记录被门控
    拦截的盘口（用于校准阈值），它们**没有投钱**。若把它们也算成本金，
    ROI 会被系统性稀释到接近 0，得出“模型不赚钱”的错误结论。
    被拦截行的表现单独放在 `unpicked_*` 里 —— 这正是判断门控是否
    在帮忙（拦掉的确实更差）还是误杀的依据。

    Args:
        rows: 台账条目序列；每条需提供 `status` / `pnl` / `odds` /
            `closing_odds` 属性（或同名键）。

    Returns:
        统计字典。
    """
    def _get(row: Any, name: str, default: Any = None) -> Any:
        if isinstance(row, dict):
            return row.get(name, default)
        return getattr(row, name, default)

    def _num(row: Any, name: str) -> float:
        """安全取数值：台账来自 JSON 落盘，字段可能是字符串或缺失。

        不能让一条脏数据把整次统计弄崩 —— 否则用户永远看不到命中率。
        """
        try:
            value = float(_get(row, name, 0.0) or 0.0)
            return value if math.isfinite(value) else 0.0
        except (TypeError, ValueError):
            return 0.0

    graded = [r for r in rows if _get(r, "status") in GRADED_STATUSES]
    pending = [r for r in rows if _get(r, "status") == SETTLE_PENDING]
    void = [r for r in rows if _get(r, "status") == SETTLE_VOID]

    # 默认 True：兼容只传结算行的调用方（它们本来就都是真下注的）
    staked = [r for r in graded if bool(_get(r, "is_pick", True))]
    unpicked = [r for r in graded if not bool(_get(r, "is_pick", True))]

    def _wins(items: Sequence[Any]) -> int:
        return sum(1 for r in items
                   if _get(r, "status") in (SETTLE_WON, SETTLE_HALF_WON))

    def _losses(items: Sequence[Any]) -> int:
        return sum(1 for r in items
                   if _get(r, "status") in (SETTLE_LOST, SETTLE_HALF_LOST))

    stake = float(len(staked))          # 每注 1 单位本金
    profit = sum(_num(r, "pnl") for r in staked)
    wins = _wins(staked)
    losses = _losses(staked)
    pushes = sum(1 for r in staked if _get(r, "status") == SETTLE_PUSH)

    # CLV：只对有收盘价的注统计
    clvs: List[float] = []
    for r in staked:
        if _get(r, "is_live", False):
            continue  # 不同赛况下的滚球价格漂移不是相同信息集 CLV
        c = clv(_num(r, "odds"), _num(r, "closing_odds"))
        if c is not None:
            clvs.append(c)

    half_won = sum(_get(r, "status") == SETTLE_HALF_WON for r in staked)
    half_lost = sum(_get(r, "status") == SETTLE_HALF_LOST for r in staked)
    win_units = wins - half_won * .5
    loss_units = losses - half_lost * .5
    out = {
        "total": len(rows),
        "graded": len(graded),
        "pending": len(pending),
        "void": len(void),
        "won": wins,
        "lost": losses,
        "push": pushes,
        # 命中率分母排除走水（走水既非赢也非输）
        "hit_rate": (round(win_units / (win_units + loss_units), 4)
                     if win_units + loss_units else None),
        "half_won": half_won,
        "half_lost": half_lost,
        "full_won": wins - half_won,
        "full_lost": losses - half_lost,
        "win_stake_units": win_units,
        "loss_stake_units": loss_units,
        "profitable_pick_rate": round(wins / (wins + losses), 4) if wins + losses else None,
        "matches": len({_get(r, "match_id") for r in rows if _get(r, "match_id")}),
        "metric_basis": "模拟每条独立建议1单位本金；命中率按赢/输半注权重，走水剔除",
        "clv_basis": "仅赛前同盘口同结果；滚球价格漂移不作为CLV",
        "profit_units": round(profit, 4),
        "stake_units": round(stake, 4),
        "roi": round(profit / stake, 6) if stake else None,
        "avg_odds": (round(sum(_num(r, "odds") for r in staked) / len(staked), 4)
                     if staked else None),
        "clv_n": len(clvs),
        "clv_mean": (round(sum(clvs) / len(clvs), 6) if clvs else None),
        "clv_positive_rate": (round(sum(1 for c in clvs if c > 0) / len(clvs), 4)
                              if clvs else None),
    }
    # 被门控拦截的盘口表现：用于回答「门控是在帮忙还是误杀」
    uw, ul = _wins(unpicked), _losses(unpicked)
    out["unpicked_n"] = len(unpicked)
    out["unpicked_hit_rate"] = (round(uw / (uw + ul), 4)
                                if (uw + ul) else None)
    return out
