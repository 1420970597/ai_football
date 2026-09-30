#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校准与评估指标 —— 报告 §8.8

报告 §8.8 的核心洞察是 **CLV 的双重角色**：

    CLV 既是「你判断自己是否真有优势」的指标，
    也是「庄家判断你是否优势玩家」的指标（§5.3）。
    同一个数字，同时是你成功的证明和你被封的原因。

本模块提供 L6 反馈闭环所需的全部度量：
    Brier 分数      概率预测的总体校准质量
    ECE             期望校准误差（分箱），检测系统性过度自信
    CLV             收盘线价值
    对数增长率      与 Kelly 目标一致的绩效度量
    最大回撤        生存能力评估

依赖：仅标准库。
"""

from __future__ import annotations

import math
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .models import CalibrationReport

__all__ = [
    "brier_score",
    "expected_calibration_error",
    "clv",
    "max_drawdown",
    "log_growth_rate",
    "build_calibration_report",
    "DEFAULT_N_BINS",
]

_EPS = 1e-12
DEFAULT_N_BINS = 10


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #

def _finite(v: object, name: str) -> float:
    try:
        out = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("%s 无法转换为数值: %r" % (name, v)) from exc
    if not math.isfinite(out):
        raise ValueError("%s 非有限值: %r" % (name, v))
    return out


def _prob(v: object, name: str) -> float:
    out = _finite(v, name)
    if out < 0.0 or out > 1.0:
        raise ValueError("%s 必须在 [0,1] 内，实际 %r" % (name, out))
    return out


def _check_len(probs: Sequence[float], outcomes: Sequence[object]) -> int:
    if len(probs) != len(outcomes):
        raise ValueError(
            "预测与结果长度不一致: %d vs %d" % (len(probs), len(outcomes))
        )
    if not probs:
        raise ValueError("输入不能为空")
    return len(probs)


# --------------------------------------------------------------------------- #
# Brier 分数
# --------------------------------------------------------------------------- #

def brier_score(
    probs: Sequence[float],
    outcomes: Sequence[object],
) -> float:
    """Brier 分数 BS = (1/N)·Σ(p_i − y_i)²（报告 §8.8）。

    越小越好：0 表示完美，0.25 相当于对二分类永远预测 0.5。

    Args:
        probs: 预测概率。
        outcomes: 实际结果（真值用 1/True，假用 0/False）。
    """
    n = _check_len(probs, outcomes)
    total = 0.0
    for i in range(n):
        p = _prob(probs[i], "probs[%d]" % i)
        y = 1.0 if _truthy(outcomes[i]) else 0.0
        total += (p - y) ** 2
    return total / n


def _truthy(v: object) -> bool:
    """把多种真值表示统一为 bool。

    支持 1/0、True/False、"win"/"lose"、"1"/"0" 等常见形式，
    以适配不同上游数据源的约定。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        # 不做无条件 float() 转换：某些数值子类型（如 Decimal 的极端值）
        # 在转换时可能抛出，统一收口为 ValueError 以保证调用方可预测。
        try:
            num = float(v)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("无法转换为数值的真值: %r" % (v,)) from exc
        if not math.isfinite(num):
            raise ValueError("真值不能为 NaN/Inf: %r" % (v,))
        return num != 0.0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "t", "yes", "y", "win", "won", "hit", "success"):
            return True
        if s in ("0", "false", "f", "no", "n", "lose", "lost", "miss", "fail"):
            return False
        raise ValueError("无法解析为真值的字符串: %r" % v)
    raise TypeError("不支持的真值类型: %r" % type(v).__name__)


# --------------------------------------------------------------------------- #
# ECE
# --------------------------------------------------------------------------- #

def expected_calibration_error(
    probs: Sequence[float],
    outcomes: Sequence[object],
    n_bins: int = DEFAULT_N_BINS,
) -> Tuple[float, Tuple[Mapping[str, object], ...]]:
    """期望校准误差 ECE = Σ_b (n_b/N)·|acc_b − conf_b|（报告 §8.8）。

    用于检测**系统性过度自信**：若模型在置信度 0.8 的样本上只有 0.6 的
    准确率，ECE 会显著大于 0。

    Args:
        probs: 预测概率。
        outcomes: 实际结果。
        n_bins: 分箱数（默认 10）。

    Returns:
        (ece, bins)，bins 为每个非空箱的明细，便于 UI 画可靠性曲线。
    """
    n = _check_len(probs, outcomes)
    if n_bins < 1:
        raise ValueError("n_bins 必须 ≥ 1，实际 %r" % n_bins)

    buckets: List[List[Tuple[float, float]]] = [[] for _ in range(n_bins)]
    for i in range(n):
        p = _prob(probs[i], "probs[%d]" % i)
        y = 1.0 if _truthy(outcomes[i]) else 0.0
        # 分箱下标：p ∈ [0,1] 且 n_bins ≥ 1，故结果必在 [0, n_bins] 内。
        # 用 math.floor 而非 int() 截断，避免负数截断向零取整的歧义；
        # 再夹取到 n_bins−1 以处理 p == 1.0 的右边界。
        idx = min(max(math.floor(p * n_bins), 0), n_bins - 1)
        buckets[idx].append((p, y))

    ece = 0.0
    bins: List[Mapping[str, object]] = []
    for b, items in enumerate(buckets):
        if not items:
            continue
        cnt = len(items)
        conf = math.fsum(p for p, _ in items) / cnt
        acc = math.fsum(y for _, y in items) / cnt
        gap = abs(acc - conf)
        ece += (cnt / n) * gap
        bins.append({
            "bin": b,
            "lo": round(b / n_bins, 6),
            "hi": round((b + 1) / n_bins, 6),
            "n": cnt,
            "confidence": round(conf, 6),
            "accuracy": round(acc, 6),
            "gap": round(gap, 6),
            "overconfident": acc < conf,
        })
    return ece, tuple(bins)


# --------------------------------------------------------------------------- #
# CLV
# --------------------------------------------------------------------------- #

def clv(bet_odds: float, close_odds: float) -> float:
    """收盘线价值 CLV = o_bet / o_close − 1（报告 §8.8）。

    CLV > 0 表示你拿到的价格优于收盘价——这是**注单质量**的核心指标。
    报告 §5.3：庄家正是用这个信号识别优势玩家。

    ⚠️ 报告 §8.8 的双重角色提醒：这个数字越大，越说明你定价对，
       也越可能触发降限额与拒单（§8.3 的 ∂q_fill/∂EV < 0）。
    """
    bet_odds = _finite(bet_odds, "bet_odds")
    close_odds = _finite(close_odds, "close_odds")
    if bet_odds <= 1.0:
        raise ValueError("bet_odds 必须 > 1.0，实际 %r" % bet_odds)
    if close_odds <= 1.0:
        raise ValueError("close_odds 必须 > 1.0，实际 %r" % close_odds)
    return bet_odds / close_odds - 1.0


def clv_mean(pairs: Sequence[Tuple[float, float]]) -> Optional[float]:
    """多注 CLV 均值。输入为空时返回 None。"""
    if not pairs:
        return None
    vals = [clv(b, c) for b, c in pairs]
    return math.fsum(vals) / len(vals)


# --------------------------------------------------------------------------- #
# 最大回撤
# --------------------------------------------------------------------------- #

def max_drawdown(equity: Sequence[float]) -> float:
    """最大回撤 max(W_peak − W_t)/W_peak（报告 §8.8）。

    Returns:
        [0,1] 内的比例；序列为空或全为 0 时返回 0.0。
    """
    if not equity:
        return 0.0

    vals = [_finite(x, "equity[%d]" % i) for i, x in enumerate(equity)]
    if any(v < 0 for v in vals):
        raise ValueError("净值序列不应包含负值")

    peak = vals[0]
    worst = 0.0
    for v in vals:
        if v > peak:
            peak = v
        if peak > _EPS:
            dd = (peak - v) / peak
            worst = max(worst, dd)
    return worst


# --------------------------------------------------------------------------- #
# 对数增长率
# --------------------------------------------------------------------------- #

def log_growth_rate(returns: Sequence[float]) -> float:
    """对数增长率 g = (1/T)·Σ log(1 + r_t)（报告 §8.6/§8.8）。"""
    if not returns:
        raise ValueError("returns 不能为空")
    total = 0.0
    for i, r in enumerate(returns):
        r = _finite(r, "returns[%d]" % i)
        if r <= -1.0:
            raise ValueError(
                "单期收益必须 > −1，returns[%d]=%r" % (i, r)
            )
        total += math.log(1.0 + r)
    return total / len(returns)


def returns_from_equity(equity: Sequence[float]) -> Tuple[float, ...]:
    """由净值序列推算逐期收益率。"""
    if len(equity) < 2:
        return ()
    vals = [_finite(x, "equity[%d]" % i) for i, x in enumerate(equity)]
    out = []
    for prev, cur in zip(vals, vals[1:]):
        if prev <= _EPS:
            out.append(0.0)
        else:
            out.append(cur / prev - 1.0)
    return tuple(out)


# --------------------------------------------------------------------------- #
# 汇总报告
# --------------------------------------------------------------------------- #

def build_calibration_report(
    probs: Sequence[float],
    outcomes: Sequence[object],
    equity: Optional[Sequence[float]] = None,
    clv_pairs: Optional[Sequence[Tuple[float, float]]] = None,
    n_bins: int = DEFAULT_N_BINS,
) -> CalibrationReport:
    """汇总生成 L6 校准报告（报告 §8.8）。

    Args:
        probs: 预测概率序列（所有注单的胜出概率）。
        outcomes: 对应实际结果。
        equity: 可选，净值序列，用于对数增长率与最大回撤。
        clv_pairs: 可选，(下注赔率, 收盘赔率) 序列。
        n_bins: ECE 分箱数。

    Returns:
        CalibrationReport
    """
    notes: List[str] = []
    n = len(probs)

    brier: Optional[float] = None
    ece: Optional[float] = None
    bins: Tuple[Mapping[str, object], ...] = ()

    if n > 0 and len(outcomes) == n:
        try:
            brier = brier_score(probs, outcomes)
        except (ValueError, TypeError) as exc:
            notes.append("Brier 计算失败：%s" % exc)
        try:
            ece, bins = expected_calibration_error(probs, outcomes, n_bins)
        except (ValueError, TypeError) as exc:
            notes.append("ECE 计算失败：%s" % exc)
    else:
        notes.append("无有效样本，Brier/ECE 置空")

    lg: Optional[float] = None
    mdd: Optional[float] = None
    if equity:
        try:
            mdd = max_drawdown(equity)
        except ValueError as exc:
            notes.append("最大回撤计算失败：%s" % exc)
        try:
            rets = returns_from_equity(equity)
            if rets:
                lg = log_growth_rate(rets)
        except ValueError as exc:
            notes.append("对数增长率计算失败：%s" % exc)

    clv_m: Optional[float] = None
    if clv_pairs:
        try:
            clv_m = clv_mean(clv_pairs)
        except ValueError as exc:
            notes.append("CLV 计算失败：%s" % exc)

    if clv_m is not None and clv_m > 0:
        notes.append(
            "CLV 均值为正（%.4f）：报告 §8.8 提醒这是双刃剑——"
            "既证明定价正确，也会触发 §5.3 的降限额与拒单" % clv_m
        )

    return CalibrationReport(
        n=n,
        brier=brier,
        ece=ece,
        log_growth=lg,
        max_drawdown=mdd,
        clv_mean=clv_m,
        bins=bins,
        notes=tuple(notes),
    )
