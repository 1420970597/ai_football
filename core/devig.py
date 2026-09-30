#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
去水（margin removal）—— 报告 §8.1

报告核心论点：**去水方法的选择本身就能造成 ±1 pp 量级的偏差，
而在这个量级上，edge 的符号会被翻转。** 因此本模块不是"选一个方法算一下"，
而是**同时算五种方法、报告它们之间的分歧、并在分歧过大时给出告警**。

五种方法（报告 §8.1 表）：
  1. Proportional   p_i = q_i / B           等比例抽税，不处理 FL bias
  2. Additive       p_i = q_i − m/n         等概率点抽税，可能产生负概率
  3. Power          解 Σ q_i^k = 1, p_i = q_i^k
  4. OddsRatio      在 log-odds 尺度做共同平移
  5. Shin           p_i = (√(z² + 4(1−z)q_i²/B) − z) / (2(1−z))，解 z 使 Σp=1

其中 Shin 内生处理 favourite-longshot bias，作为默认基准。

依赖：仅标准库（AGENTS.md §1）。
"""

from __future__ import annotations

import math
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .models import DevigMethod, FairProbabilities, OddsSnapshot

__all__ = [
    "devig",
    "proportional",
    "additive",
    "power",
    "odds_ratio",
    "shin",
    "method_spread_pp",
    "DEFAULT_SPREAD_WARN_PP",
    "ALL_METHODS",
]

# 报告 §8.1：实测方法间 L1 偏差最大 8.0 pp（AC米兰 vs 莱切）。
# 1.0 pp 是保守告警线——超过它，edge 符号已不可信。
DEFAULT_SPREAD_WARN_PP = 1.0

ALL_METHODS: Tuple[DevigMethod, ...] = (
    DevigMethod.PROPORTIONAL,
    DevigMethod.ADDITIVE,
    DevigMethod.POWER,
    DevigMethod.ODDS_RATIO,
    DevigMethod.SHIN,
)

_EPS = 1e-12
_MAX_ITER = 200
_TOL = 1e-12


# --------------------------------------------------------------------------- #
# 二分求根工具
# --------------------------------------------------------------------------- #

def _bisect(func, lo: float, hi: float,
            tol: float = _TOL, max_iter: int = _MAX_ITER) -> float:
    """在 [lo, hi] 上对单调函数二分求根。

    func 需在 lo 与 hi 处异号。返回使 |func| 最小的 x。
    使用二分而非牛顿法：无需导数，且对本问题的单调函数稳定收敛。
    """
    if not (math.isfinite(lo) and math.isfinite(hi)):
        raise ValueError("二分区间必须是有限值")
    if lo >= hi:
        raise ValueError("二分区间需满足 lo < hi，实际 lo=%r hi=%r" % (lo, hi))

    f_lo, f_hi = func(lo), func(hi)
    if not (math.isfinite(f_lo) and math.isfinite(f_hi)):
        raise ValueError("二分端点函数值非有限")
    if f_lo == 0.0:
        return lo
    if f_hi == 0.0:
        return hi
    if (f_lo > 0) == (f_hi > 0):
        # 端点同号：仍返回较接近 0 的一端，由调用方判断是否可接受
        return lo if abs(f_lo) < abs(f_hi) else hi

    mid = lo
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = func(mid)
        if abs(f_mid) < tol or (hi - lo) < tol:
            return mid
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi, f_hi = mid, f_mid
    return mid


def _normalize(p: Sequence[float]) -> Tuple[float, ...]:
    """把非负序列归一化到和为 1；全零时退化为均匀分布。"""
    total = math.fsum(p)
    if total <= _EPS:
        n = len(p)
        return tuple(1.0 / n for _ in range(n))
    return tuple(x / total for x in p)


# --------------------------------------------------------------------------- #
# 五种方法
# --------------------------------------------------------------------------- #

def proportional(q: Sequence[float]) -> Tuple[float, ...]:
    """比例归一化：p_i = q_i / B。

    最简单、最常用，但**不处理 favourite-longshot bias**，
    会系统性高估冷门（报告 §8.1 实测冷门偏差最大）。
    """
    return _normalize(q)


def additive(q: Sequence[float]) -> Tuple[float, ...]:
    """加法法：p_i = q_i − m/n。

    缺陷：当某个 q_i 很小时，可能得到**负概率**（报告 §8.1 明确列出该缺陷）。
    本实现允许负值通过（以保证方法原貌），但调用方应通过
    `_additive_is_valid` 或返回的告警得知。
    """
    n = len(q)
    if n == 0:
        raise ValueError("q 不能为空")
    m = math.fsum(q) - 1.0
    return tuple(x - m / n for x in q)


def additive_is_valid(p: Sequence[float]) -> bool:
    """加法法结果是否存在负概率。"""
    return all(x >= -_EPS for x in p)


def power(q: Sequence[float], max_iter: int = _MAX_ITER) -> Tuple[float, ...]:
    """幂法：解 Σ q_i^k = 1，取 p_i = q_i^k。

    k > 1 时把更多概率推向热门（与 FL bias 方向一致）。
    对 B > 1（正常有税市场）k 的搜索区间取 (1e-6, 10]。
    """
    def f(k: float) -> float:
        return math.fsum(x ** k for x in q) - 1.0

    k = _bisect(f, 1e-6, 10.0, max_iter=max_iter)
    return _normalize(tuple(x ** k for x in q))


def odds_ratio(q: Sequence[float]) -> Tuple[float, ...]:
    """赔率比法：在 log-odds 尺度做共同平移。

    令 logit(p_i) = logit(q_i) + c，解 c 使 Σ p_i = 1。
    c < 0 表示整体收缩（去税）。
    """
    def f(c: float) -> float:
        total = 0.0
        for x in q:
            x = min(max(x, _EPS), 1.0 - _EPS)
            z = math.log(x / (1.0 - x)) + c
            p = 1.0 / (1.0 + math.exp(-z))
            total += p
        return total - 1.0

    c = _bisect(f, -20.0, 0.0)
    out = []
    for x in q:
        x = min(max(x, _EPS), 1.0 - _EPS)
        z = math.log(x / (1.0 - x)) + c
        out.append(1.0 / (1.0 + math.exp(-z)))
    return _normalize(out)


def shin(q: Sequence[float],
         max_iter: int = _MAX_ITER) -> Tuple[Tuple[float, ...], float]:
    """Shin (1992, 1993) 模型。

    p_i = (√(z² + 4(1−z)·q_i²/B) − z) / (2(1−z))，z 由 Σp_i = 1 数值求解。
    z 是内幕交易者比例的估计，内生处理 favourite-longshot bias。

    Returns:
        (probabilities, z)
    """
    B = math.fsum(q)
    if B <= _EPS:
        raise ValueError("booksum 必须为正，实际 %r" % B)

    def f(z: float) -> float:
        z = min(max(z, 0.0), 1.0 - 1e-9)
        denom = 2.0 * (1.0 - z)
        total = 0.0
        for x in q:
            inner = z * z + 4.0 * (1.0 - z) * (x * x) / B
            total += (math.sqrt(max(inner, 0.0)) - z) / denom
        return total - 1.0

    z = _bisect(f, 0.0, 0.5)
    z = min(max(z, 0.0), 1.0 - 1e-9)
    denom = 2.0 * (1.0 - z)
    p = []
    for x in q:
        inner = z * z + 4.0 * (1.0 - z) * (x * x) / B
        p.append((math.sqrt(max(inner, 0.0)) - z) / denom)
    return _normalize(p), z


# --------------------------------------------------------------------------- #
# 分歧度量
# --------------------------------------------------------------------------- #

def method_spread_pp(per_method: Mapping[str, Sequence[float]]) -> float:
    """各方法之间的最大 L1 偏差，单位：百分点。

    L1 距离 = Σ|p_a,i − p_b,i|，报告 §8.1 用同一口径。
    入参用 Mapping 而非 Dict：调用方常持有 Dict[str, List[float]]，
    而 Dict 在值类型上是不变的（invariant），Mapping 则为协变。
    """
    keys = list(per_method)
    if len(keys) < 2:
        return 0.0
    worst = 0.0
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = per_method[keys[i]], per_method[keys[j]]
            if len(a) != len(b):
                continue
            d = math.fsum(abs(x - y) for x, y in zip(a, b)) * 100.0
            worst = max(worst, d)
    return worst


def _as_finite_float(value: object, name: str) -> float:
    """安全浮点转换：任何异常都转为可读的 ValueError。

    去水结果理论上已是 float，但为防上游传入 Decimal / numpy 标量
    或异常对象，统一在此收口，避免未捕获的 TypeError 冒泡到 API 层。
    """
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("%s 无法转换为数值: %r" % (name, value)) from exc
    if not math.isfinite(out):
        raise ValueError("%s 非有限值: %r" % (name, value))
    return out


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def devig(
    snapshot: OddsSnapshot,
    method: DevigMethod = DevigMethod.AUTO,
    spread_warn_pp: float = DEFAULT_SPREAD_WARN_PP,
) -> FairProbabilities:
    """对快照去水，返回公平概率与诊断信息。

    Args:
        snapshot: 赔率快照。停盘/陈旧状态会被标注但**仍允许计算**
            （调用方应据 `state` 自行决定是否使用）。
        method: 去水方法；AUTO 时以 Shin 为基准，失败则回退 Proportional。
        spread_warn_pp: 方法间 L1 偏差告警阈值（百分点）。

    Returns:
        FairProbabilities

    Raises:
        ValueError: 赔率非法（如 booksum ≤ 0）。
    """
    q = snapshot.raw_implied
    B = math.fsum(q)
    if B <= _EPS:
        raise ValueError("booksum 必须为正，实际 %r" % B)

    notes: List[str] = []

    if snapshot.state is not snapshot.state.ACTIVE:
        notes.append(
            "快照状态为 %s：报告 §5.3/§7.1 规定该价格的 edge 不可信，"
            "结果仅供观察" % snapshot.state.value
        )

    per: Dict[str, List[float]] = {}

    # -- 逐方法计算（任一方法失败不影响其它方法） ---------------------------
    per[DevigMethod.PROPORTIONAL.value] = list(proportional(q))

    add = additive(q)
    per[DevigMethod.ADDITIVE.value] = list(add)
    if not additive_is_valid(add):
        notes.append("加法法产生负概率，该结果不可用（报告 §8.1 已列出此缺陷）")

    try:
        per[DevigMethod.POWER.value] = list(power(q))
    except (ValueError, OverflowError, ZeroDivisionError) as exc:
        notes.append("幂法求解失败，已跳过：%s" % exc)

    try:
        per[DevigMethod.ODDS_RATIO.value] = list(odds_ratio(q))
    except (ValueError, OverflowError, ZeroDivisionError) as exc:
        notes.append("赔率比法求解失败，已跳过：%s" % exc)

    shin_z: Optional[float] = None
    try:
        p_shin, shin_z = shin(q)
        per[DevigMethod.SHIN.value] = list(p_shin)
    except (ValueError, OverflowError, ZeroDivisionError) as exc:
        notes.append("Shin 求解失败，已跳过：%s" % exc)

    spread = method_spread_pp(per)
    warn = spread > spread_warn_pp
    if warn:
        notes.append(
            "方法间 L1 偏差 %.3f pp 超过阈值 %.3f pp："
            "报告 §8.1 指出该量级足以翻转 edge 符号，切勿据此计算 edge"
            % (spread, spread_warn_pp)
        )
    if B < 1.0:
        notes.append(
            "booksum %.6f < 1（疑似套利或解析错误），水钱为负：%0.6f" % (B, B - 1.0)
        )

    # -- 选择最终方法 -------------------------------------------------------
    chosen = method
    if chosen is DevigMethod.AUTO:
        chosen = (DevigMethod.SHIN if DevigMethod.SHIN.value in per
                  else DevigMethod.PROPORTIONAL)
        if DevigMethod.SHIN.value not in per:
            notes.append("Shin 不可用，AUTO 回退到比例归一化")

    selected = per.get(chosen.value)
    if selected is None:
        # 显式指定了失败的方法：回退并说明
        notes.append("指定的 %s 不可用，回退到比例归一化" % chosen.value)
        chosen = DevigMethod.PROPORTIONAL
        selected = per[DevigMethod.PROPORTIONAL.value]

    # 加法法负概率时不允许作为最终结果
    if chosen is DevigMethod.ADDITIVE and not additive_is_valid(selected):
        notes.append("加法法含负概率，最终结果已回退到比例归一化")
        chosen = DevigMethod.PROPORTIONAL
        selected = per[DevigMethod.PROPORTIONAL.value]

    return FairProbabilities(
        probabilities=tuple(
            _as_finite_float(x, "probabilities[%d]" % i)
            for i, x in enumerate(selected)
        ),
        method=chosen,
        margin=B - 1.0,
        outcomes=snapshot.outcomes,
        shin_z=shin_z,
        method_spread_pp=spread,
        spread_warning=warn,
        per_method={
            k: tuple(_as_finite_float(x, "%s[%d]" % (k, i))
                     for i, x in enumerate(v))
            for k, v in per.items()
        },
        notes=tuple(notes),
    )
