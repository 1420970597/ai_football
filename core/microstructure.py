#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微结构信号提取 —— 报告 §5.1

报告 §5.1 指出：微结构信号是本方案中**唯一不含「报价撤回」污染**的信息维度，
因为你不是在比谁更快读到同一个数字，而是在比谁更快识别**数字变化的方式**。

五个信号：
    跳动频率 tick_frequency    单位时间赔率变动次数 → 信息正在流入
    恢复时间 recovery_seconds  跳价后回到新稳态的时长 → 冲击大小
    漂移速率 drift_rate        定向变化速度 → 方向性信息
    离散度     cross-book spread（见 spread_across_books）
    停盘时长   suspension_seconds → 重大事件，恢复后价格失真

**必须同时承认边界**（报告 §5.1 原文）：
> 微结构信号在流动性充足、撮合连续的市场中信息含量高；在庄家单方面报价、
> 可随时撤单的市场中，信号会被"报价撤回"污染 —— 你观察到的"跳动"很大一部分
> 不是市场信号，而是对手方风控行为。

因此本模块**显式统计停盘次数与时长**，并在停盘占比过高时把信号标记为不可用。

依赖：仅标准库。
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from .models import MicrostructureSignal, OddsSnapshot, SnapshotState

__all__ = [
    "extract_signals",
    "cross_book_spread",
    "trend_direction",
    "is_suspended_heavy",
    "SUSPENSION_HEAVY_RATIO",
]

_EPS = 1e-12

# 停盘时长占窗口比例超过该阈值时，认为信号已被风控行为主导（报告 §5.1）
SUSPENSION_HEAVY_RATIO = 0.30


def _finite(v: object, name: str) -> float:
    try:
        out = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("%s 无法转换为数值: %r" % (name, v)) from exc
    if not math.isfinite(out):
        raise ValueError("%s 非有限值: %r" % (name, v))
    return out


def is_suspended_heavy(suspension_seconds: float, window_seconds: float) -> bool:
    """停盘时长是否已主导该窗口（报告 §5.1 的污染判据）。"""
    susp = _finite(suspension_seconds, "suspension_seconds")
    win = _finite(window_seconds, "window_seconds")
    if win <= _EPS:
        return True
    return (susp / win) > SUSPENSION_HEAVY_RATIO


def _sort_by_time(snaps: Sequence[OddsSnapshot]) -> List[OddsSnapshot]:
    """按采集时间排序；用于计算时序特征。"""
    return sorted(snaps, key=lambda s: s.captured_at)


def extract_signals(
    snapshots: Sequence[OddsSnapshot],
    outcome_index: int = 0,
    min_ticks_for_recovery: int = 2,
) -> MicrostructureSignal:
    """从同一结果的时间序列快照中提取微结构信号。

    Args:
        snapshots: 同一 match_id / market / outcome 的快照序列（顺序不限）。
        outcome_index: 关注的 result 下标（默认 0，即主胜）。
        min_ticks_for_recovery: 计算恢复时间所需的最少跳动次数。

    Returns:
        MicrostructureSignal。若快照为空则返回零值信号并标注 usable=False。

    Raises:
        ValueError: outcome_index 越界，或快照不属于同一 match_id。
    """
    if not snapshots:
        return MicrostructureSignal(
            match_id="", outcome="", tick_count=0, tick_frequency=0.0,
            recovery_seconds=None, drift_rate=0.0, suspension_seconds=0.0,
            n_suspended=0, window_seconds=0.0, usable=False,
            notes=("快照序列为空",),
        )

    ids = {s.match_id for s in snapshots}
    if len(ids) != 1:
        raise ValueError("快照必须属于同一 match_id，实际包含 %d 个" % len(ids))
    match_id = snapshots[0].match_id
    n_out = len(snapshots[0].outcomes)
    if not (0 <= outcome_index < n_out):
        raise ValueError(
            "outcome_index 越界：%d 不在 [0,%d)" % (outcome_index, n_out)
        )

    ordered = _sort_by_time(snapshots)
    outcome = ordered[0].outcomes[outcome_index]
    notes: List[str] = []

    # -- 停盘统计（先做，因为决定 usable） ---------------------------------
    n_suspended = 0
    suspension_seconds = 0.0
    for snap_cur, snap_next in zip(ordered, ordered[1:]):
        if snap_cur.state is SnapshotState.SUSPENDED:
            n_suspended += 1
            dt = (snap_next.captured_at - snap_cur.captured_at).total_seconds()
            if dt > 0:
                suspension_seconds += dt

    t0 = ordered[0].captured_at
    t1 = ordered[-1].captured_at
    window_seconds = (t1 - t0).total_seconds()

    # -- 跳动序列：仅取可用状态、且赔率确实变化 ----------------------------
    ticks: List[Tuple[float, float]] = []   # (相对时间, 赔率)
    for s in ordered:
        if not s.is_usable():
            continue
        ticks.append(((s.captured_at - t0).total_seconds(), s.odds[outcome_index]))

    tick_count = 0
    tick_frequency = 0.0
    if len(ticks) >= 2:
        # 注意：此处的循环变量是 float 赔率，与上面的快照循环变量区分命名，
        # 否则 mypy 会因重用同名变量而推断出错误的联合类型。
        for (_, odds_prev), (_, odds_cur) in zip(ticks, ticks[1:]):
            if abs(odds_cur - odds_prev) > _EPS:
                tick_count += 1
        span = ticks[-1][0] - ticks[0][0]
        if span > _EPS:
            tick_frequency = tick_count / span

    # -- 恢复时间：跳价后回到新稳态所需时间 --------------------------------
    recovery: Optional[float] = None
    if tick_count >= min_ticks_for_recovery and len(ticks) >= 3:
        gaps: List[float] = []
        for k in range(1, len(ticks) - 1):
            prev_o, cur_o, next_o = ticks[k - 1][1], ticks[k][1], ticks[k + 1][1]
            jumped = abs(cur_o - prev_o) > _EPS
            if not jumped:
                continue
            # 恢复：从跳价点位到后续价格再次稳定（相对变动小于跳幅的 10%）
            jump_size = abs(cur_o - prev_o)
            for m in range(k + 1, len(ticks)):
                if abs(ticks[m][1] - next_o) < 0.1 * jump_size:
                    gaps.append(ticks[m][0] - ticks[k][0])
                    break
        if gaps:
            recovery = math.fsum(gaps) / len(gaps)

    # -- 漂移速率：稳健的端点斜率 ------------------------------------------
    drift = 0.0
    if len(ticks) >= 2:
        span = ticks[-1][0] - ticks[0][0]
        if span > _EPS:
            drift = (ticks[-1][1] - ticks[0][1]) / span

    usable = True
    if suspension_seconds > 0:
        if is_suspended_heavy(suspension_seconds, window_seconds):
            usable = False
            notes.append(
                "停盘时长 %.1fs 占窗口 %.1f%%，超过阈值 %.0f%%："
                "报告 §5.1 指出此时读取的跳动已被对手方风控行为主导，信号不可用"
                % (suspension_seconds,
                   (suspension_seconds / window_seconds * 100.0)
                   if window_seconds > _EPS else 100.0,
                   SUSPENSION_HEAVY_RATIO * 100)
            )
        else:
            notes.append(
                "窗口内累计停盘 %.1fs（%d 次），报告 §5.3：停盘期为冻结值，"
                "已从跳动统计中剔除" % (suspension_seconds, n_suspended)
            )

    if tick_count == 0:
        notes.append("窗口内未观察到赔率跳动，微结构信号为空")

    return MicrostructureSignal(
        match_id=match_id,
        outcome=outcome,
        tick_count=tick_count,
        tick_frequency=tick_frequency,
        recovery_seconds=recovery,
        drift_rate=drift,
        suspension_seconds=suspension_seconds,
        n_suspended=n_suspended,
        window_seconds=window_seconds,
        usable=usable,
        notes=tuple(notes),
    )


def cross_book_spread(
    per_book_probs: Dict[str, Sequence[float]],
    outcome_index: int = 0,
) -> float:
    """跨平台/跨书商的去水后概率离散度（报告 §5.1）。

    报告 §5.1：离散度扩大 = 定价分歧，可能是套利窗口。

    Args:
        per_book_probs: {书商名: 概率序列}，**应为去水后的概率**。
        outcome_index: 关注的结果下标。

    Returns:
        最大概率 − 最小概率；不足两个来源时返回 0.0。

    Raises:
        ValueError: 下标越界。
    """
    vals: List[float] = []
    for name, probs in per_book_probs.items():
        if not probs:
            continue
        if not (0 <= outcome_index < len(probs)):
            raise ValueError(
                "outcome_index 越界：%s 只有 %d 个结果" % (name, len(probs))
            )
        vals.append(_finite(probs[outcome_index], "%s[%d]" % (name, outcome_index)))

    if len(vals) < 2:
        return 0.0
    return max(vals) - min(vals)


def trend_direction(signals: Sequence[MicrostructureSignal]) -> int:
    """由多个结果的漂移速率判断市场方向。

    Returns:
        +1 表示主胜方向赔率上行（概率下行），-1 相反，0 为无明显方向。

    说明：赔率上行意味着市场认为该结果概率下降，故方向语义与概率相反。
    本函数只做符号统计，不构成预测。
    """
    up = down = 0
    for s in signals:
        if not s.usable:
            continue
        if s.drift_rate > _EPS:
            up += 1
        elif s.drift_rate < -_EPS:
            down += 1
    if up > down:
        return 1
    if down > up:
        return -1
    return 0
