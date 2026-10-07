#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
经济学算法栈 —— 报告 §8.2–§8.7

本模块是报告 §7「L4 经济学算法栈」的代码化，六个阶段：

  S2 优势估计       edge = p·o − 1                              §8.2
  S3 不确定性收缩   p̂ = w·p_hat + (1−w)·p_prior                §8.4
  S3′ 有效期望      EV_eff = q_fill·EV − cost_exec             §8.3
  S4 仓位           分数 Kelly / 多元 Kelly f* = Σ⁻¹μ          §8.5
  S5 组合约束       相关性惩罚与敞口上限                        §8.5/§8.6
  S6 成本摊销       覆盖固定成本所需注数                        §8.7

**必须重申的上限（报告 §7.1）**：
本模块全部是「给定 p_model 之后」的最优处理。它能最大化真实优势的
几何增长率，但**无法创造优势**。若 p_model 无信息，整条栈只是在更精细地
分配一个负期望。

依赖：仅标准库。
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from .models import (
    SizingResult,
)

__all__ = [
    "implied_probabilities",
    "expected_value",
    "expected_value_from_margin",
    "edge_from_probabilities",
    "shrink_probabilities",
    "effective_ev",
    "q_fill_model",
    "kelly_fraction",
    "fractional_kelly",
    "naive_overbet_factor",
    "multivariate_kelly",
    "solve_linear_system",
    "sizing",
    "breakeven_bets",
    "log_growth_rate",
    "ruin_probability_bound",
]

_EPS = 1e-12


# --------------------------------------------------------------------------- #
# §8.2 隐含概率与期望值
# --------------------------------------------------------------------------- #

def implied_probabilities(odds: Sequence[float]) -> Tuple[float, ...]:
    """隐含概率 q_i = 1/o_i（未去水）。"""
    out = []
    for i, o in enumerate(odds):
        o = _finite(o, "odds[%d]" % i)
        if o <= 1.0:
            raise ValueError("赔率必须 > 1.0，odds[%d]=%r" % (i, o))
        out.append(1.0 / o)
    return tuple(out)


def expected_value(p: float, o: float) -> float:
    """单注期望收益率 EV = p·o − 1（以本金为基准）。"""
    p = _prob(p, "p")
    o = _finite(o, "o")
    if o <= 1.0:
        raise ValueError("赔率必须 > 1.0，实际 %r" % o)
    return p * o - 1.0


def expected_value_from_margin(margin: float) -> float:
    """公平定价下的期望值 EV = −m/(1+m)（报告 §8.1 推导）。

    这就是「报价里含的税」的数学形状：与你看好谁、模型多聪明完全无关。
    """
    m = _finite(margin, "margin")
    if m <= -1.0:
        raise ValueError("水钱 m 必须 > −1，实际 %r" % m)
    return -m / (1.0 + m)


def edge_from_probabilities(
    p_model: Sequence[float],
    odds: Sequence[float],
) -> Tuple[float, ...]:
    """逐结果计算 edge_i = p_i·o_i − 1。"""
    if len(p_model) != len(odds):
        raise ValueError(
            "p_model 与 odds 长度不一致: %d vs %d" % (len(p_model), len(odds))
        )
    return tuple(
        expected_value(p, o) for p, o in zip(p_model, odds)
    )


# --------------------------------------------------------------------------- #
# §8.4 贝叶斯收缩
# --------------------------------------------------------------------------- #

def shrink_probabilities(
    p_hat: Sequence[float],
    p_prior: Sequence[float],
    n: int,
    k: Optional[float] = None,
    p_for_k: Optional[float] = None,
    sigma_p: Optional[float] = None,
) -> Tuple[Tuple[float, ...], float]:
    """贝叶斯收缩（报告 §8.4）。

        p̂_shrink = w·p_hat + (1−w)·p_prior,  w = n/(n+k)

    其中 k 是「强度」，等价于"你对这个估计有多不确定"：
      k → 0  无收缩（完全信任模型，激进）
      k 增大  自动趋于先验（保守）

    若未显式给出 k，则按 Beta-二项形式由 `p_for_k` 与 `sigma_p` 推出：

        k = p(1−p)/σ_p² − 1

    Args:
        p_hat: 模型原始概率。
        p_prior: 先验（通常用去水后的公平概率）。
        n: 有效样本量。
        k: 收缩强度（优先使用）。
        p_for_k: 计算 k 用的基准概率（默认取 p_hat 均值）。
        sigma_p: 概率估计的标准差（与 p_for_k 配合）。

    Returns:
        (收缩后概率, 实际使用的 w)
    """
    if len(p_hat) != len(p_prior):
        raise ValueError(
            "p_hat 与 p_prior 长度不一致: %d vs %d" % (len(p_hat), len(p_prior))
        )
    if n < 0:
        raise ValueError("样本量 n 不能为负，实际 %r" % n)

    if k is None:
        if p_for_k is not None and sigma_p is not None:
            pp = _prob(p_for_k, "p_for_k")
            sp = _finite(sigma_p, "sigma_p")
            if sp <= 0:
                raise ValueError("sigma_p 必须 > 0，实际 %r" % sp)
            k = max(pp * (1.0 - pp) / (sp * sp) - 1.0, 0.0)
        else:
            k = 0.0  # 未提供不确定度信息则不收缩

    k = max(_finite(k, "k"), 0.0)
    w = 1.0 if (n + k) <= _EPS else n / (n + k)
    w = min(max(w, 0.0), 1.0)

    out = tuple(
        w * _prob(a, "p_hat[%d]" % i) + (1.0 - w) * _prob(b, "p_prior[%d]" % i)
        for i, (a, b) in enumerate(zip(p_hat, p_prior))
    )
    return _normalize_tuple(out), w


# --------------------------------------------------------------------------- #
# §8.3 有效期望（含执行过滤）
# --------------------------------------------------------------------------- #

def q_fill_model(
    ev: float,
    stake: float,
    base_q: float = 0.95,
    ev_sensitivity: float = 2.0,
    stake_sensitivity: float = 1e-4,
    stake_ref: float = 100.0,
) -> float:
    """成交概率估计 q_fill（报告 §8.3）。

    报告 §8.3 的核心断言是**两个负偏导**：
        ∂q_fill/∂EV < 0     优势越大越易被拒单（庄家用 CLV 识别优势玩家）
        ∂q_fill/∂s  < 0     注额越大越易被降限额（stake factoring）

    本函数用指数衰减形式把它们显式建模：

        q_fill = clip( base_q · exp(−a·max(EV,0)) · exp(−b·max(s − s_ref, 0)), q_min, 1 )

    ⚠️ 参数为**量级示意**，非实测值。报告 §11.3 已声明：该类参数的
       敏感性方向明确（越悲观则方案越不可行），但绝对值不应被当作估计。
    """
    ev = _finite(ev, "ev")
    stake = _finite(stake, "stake")
    base_q = _prob(base_q, "base_q")
    if stake < 0:
        raise ValueError("stake 不能为负，实际 %r" % stake)

    pos_ev = max(ev, 0.0)
    excess = max(stake - stake_ref, 0.0)
    factor = math.exp(-ev_sensitivity * pos_ev) * math.exp(-stake_sensitivity * excess)
    q = base_q * factor
    return min(max(q, 0.01), 1.0)


def effective_ev(
    ev: float,
    q_fill: float,
    cost_exec: float = 0.0,
    stake_frac: float = 1.0,
) -> float:
    """有效期望 EV_eff = q_fill·EV − cost_exec（报告 §8.3）。

    Args:
        ev: 名义期望收益率。
        q_fill: 成交概率。
        cost_exec: 单次执行成本（以本金比例表示，如 0.001 表示 0.1%）。
        stake_frac: 该注占本金的比例——成本相对本金是按此缩放的。
    """
    ev = _finite(ev, "ev")
    q_fill = _prob(q_fill, "q_fill")
    cost_exec = _finite(cost_exec, "cost_exec")
    return q_fill * ev - cost_exec


# --------------------------------------------------------------------------- #
# §8.5 Kelly
# --------------------------------------------------------------------------- #

def kelly_fraction(p: float, o: float) -> float:
    """单注全 Kelly：f* = (b·p − q)/b，b = o − 1。

    返回值可能为负（表示应做对手方向）；调用方通常应在 f* ≤ 0 时放弃该注。
    注意 **全 Kelly 假设 p 已知且准确**——参数不确定时应使用分数 Kelly
    或先做收缩（报告 §8.4/§8.6）。
    """
    p = _prob(p, "p")
    o = _finite(o, "o")
    if o <= 1.0:
        raise ValueError("赔率必须 > 1.0，实际 %r" % o)
    b = o - 1.0
    q = 1.0 - p
    return (b * p - q) / b


def fractional_kelly(p: float, o: float, lam: float = 0.25) -> float:
    """分数 Kelly：f = λ·f*，λ 常取 0.2–0.5（报告 §8.5）。

    λ < 1 是对「参数不确定 + 模型高估」的风控折扣。
    """
    lam = _finite(lam, "lam")
    if lam < 0:
        raise ValueError("λ 不能为负，实际 %r" % lam)
    return lam * kelly_fraction(p, o)


def naive_overbet_factor(rho: float) -> float:
    """naive 逐注独立 Kelly 相对最优解的**高估**倍数 ≈ 1/(1−ρ)（报告 §8.5）。

    报告 §8.5 数值：ρ=0.4 → 1.67×，ρ=0.6 → 2.50×。
    同场多市场共享球员状态/天气/裁判/节奏等风险因子，ρ 天然很高。
    """
    rho = _finite(rho, "rho")
    if rho >= 1.0:
        raise ValueError("相关系数 ρ 必须 < 1，实际 %r" % rho)
    if rho <= 0.0:
        return 1.0
    return 1.0 / (1.0 - rho)


def solve_linear_system(
    a: Sequence[Sequence[float]],
    b: Sequence[float],
) -> Tuple[float, ...]:
    """高斯消元（带部分主元）求解 A·x = b。

    用于多元 Kelly 的 f* = Σ⁻¹μ（报告 §8.5）。手写而非引入 numpy：
    核心层必须零依赖（AGENTS.md §1）。

    Raises:
        ValueError: 维度不匹配或矩阵奇异。
    """
    n = len(b)
    if n == 0:
        raise ValueError("b 不能为空")
    if len(a) != n or any(len(row) != n for row in a):
        raise ValueError("A 必须是 n×n 方阵，n=%d" % n)

    # 拷贝为可变结构
    m = [[_finite(a[i][j], "A[%d][%d]" % (i, j)) for j in range(n)]
         for i in range(n)]
    rhs = [_finite(b[i], "b[%d]" % i) for i in range(n)]

    for col in range(n):
        # 部分主元
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-14:
            raise ValueError("矩阵奇异（第 %d 列主元接近 0）" % col)
        if piv != col:
            m[col], m[piv] = m[piv], m[col]
            rhs[col], rhs[piv] = rhs[piv], rhs[col]

        inv = 1.0 / m[col][col]
        for r in range(col + 1, n):
            f = m[r][col] * inv
            if f == 0.0:
                continue
            for c in range(col, n):
                m[r][c] -= f * m[col][c]
            rhs[r] -= f * rhs[col]

    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        acc = rhs[r] - math.fsum(m[r][c] * x[c] for c in range(r + 1, n))
        x[r] = acc / m[r][r]
    return tuple(x)


def multivariate_kelly(
    mu: Sequence[float],
    cov: Sequence[Sequence[float]],
    lam: float = 0.25,
) -> Tuple[float, ...]:
    """多元 Kelly：f* = λ·Σ⁻¹μ（报告 §8.5）。

    Args:
        mu: 各注的超额收益向量（即 edge）。
        cov: 收益的协方差矩阵。
        lam: 分数 Kelly 系数。

    Returns:
        各注的仓位比例（可能为负，调用方通常截断到 0）。
    """
    if len(mu) != len(cov):
        raise ValueError("mu 与 cov 维度不匹配: %d vs %d" % (len(mu), len(cov)))
    lam = max(_finite(lam, "lam"), 0.0)
    raw = solve_linear_system(cov, mu)
    return tuple(lam * v for v in raw)


def build_covariance(
    fracs: Sequence[float],
    probs: Sequence[float],
    odds: Sequence[float],
    rho: float = 0.0,
) -> Tuple[Tuple[float, ...], ...]:
    """构造组合收益协方差矩阵（报告 §8.5 的解析式）。

    对角项：  f_i²·p_i(1−p_i)·o_i²
    协方差：  2ρ·f_i·f_j·o_i·o_j·√(p_i(1−p_i)·p_j(1−p_j))

    这里返回的是「给定仓位下的收益协方差」。用于独立评估风险；
    多元 Kelly 求解时则用单位仓位的协方差。
    """
    _check_same_len(fracs, probs, odds)
    n = len(probs)
    out: List[List[float]] = [[0.0] * n for _ in range(n)]
    for i in range(n):
        pi = _prob(probs[i], "probs[%d]" % i)
        oi = _finite(odds[i], "odds[%d]" % i)
        fi = _finite(fracs[i], "fracs[%d]" % i)
        out[i][i] = (fi ** 2) * pi * (1.0 - pi) * (oi ** 2)
        for j in range(i + 1, n):
            pj = _prob(probs[j], "probs[%d]" % j)
            oj = _finite(odds[j], "odds[%d]" % j)
            fj = _finite(fracs[j], "fracs[%d]" % j)
            v = (2.0 * _finite(rho, "rho") * fi * fj * oi * oj
                 * math.sqrt(max(pi * (1.0 - pi) * pj * (1.0 - pj), 0.0)))
            out[i][j] = out[j][i] = v
    return tuple(tuple(row) for row in out)


def portfolio_std(cov: Sequence[Sequence[float]]) -> float:
    """组合收益标准差 σ = √(ΣΣ cov_ij)。"""
    total = 0.0
    for row in cov:
        total += math.fsum(row)
    return math.sqrt(max(total, 0.0))


# --------------------------------------------------------------------------- #
# 高层封装：sizing
# --------------------------------------------------------------------------- #

def sizing(
    outcomes: Sequence[str],
    probs: Sequence[float],
    odds: Sequence[float],
    edges: Sequence[float],
    lam: float = 0.25,
    rho: float = 0.0,
    max_total_exposure: float = 0.25,
    min_edge: float = 0.0,
) -> SizingResult:
    """仓位建议（研究报告用，不构成投注建议）。

    流程：
      1. 对每个正 edge 的注算分数 Kelly
      2. 若提供 rho，则用多元 Kelly（含协方差）替代逐注独立计算
      3. 施加总敞口上限

    Args:
        outcomes: 结果名。
        probs: 收缩后的模型概率。
        odds: 赔率。
        edges: 各注 edge。
        lam: 分数 Kelly 系数。
        rho: 同场/相关注之间的相关系数；> 0 时启用多元求解。
        max_total_exposure: 总敞口上限（占银行比例）。
        min_edge: 低于该 edge 直接置零。
    """
    _check_same_len(outcomes, probs, odds, edges)
    n = len(outcomes)
    notes: List[str] = []

    if not (0.0 < max_total_exposure <= 1.0):
        raise ValueError("max_total_exposure 必须在 (0,1] 内")
    lam = _finite(lam, "lam")
    rho = _finite(rho, "rho")
    if not (-1.0 < rho < 1.0):
        raise ValueError("rho 必须在 (−1,1) 内，实际 %r" % rho)

    # 1) 分数 Kelly 作为基线
    base = []
    for i in range(n):
        e = _finite(edges[i], "edges[%d]" % i)
        if e <= _finite(min_edge, "min_edge"):
            base.append(0.0)
            continue
        base.append(max(fractional_kelly(probs[i], odds[i], lam), 0.0))

    used_cov = False
    if rho > 0.0 and any(f > 0 for f in base):
        used_cov = True
        # 单位仓位的收益协方差：Var_i = p(1−p)o²，Cov_ij = ρ·o_i·o_j·√(...)
        cov: List[List[float]] = [[0.0] * n for _ in range(n)]
        for i in range(n):
            pi = _prob(probs[i], "probs[%d]" % i)
            oi = _finite(odds[i], "odds[%d]" % i)
            cov[i][i] = max(pi * (1.0 - pi) * oi * oi, _EPS)
            for j in range(i + 1, n):
                pj = _prob(probs[j], "probs[%d]" % j)
                oj = _finite(odds[j], "odds[%d]" % j)
                c = (rho * oi * oj
                     * math.sqrt(max(pi * (1.0 - pi) * pj * (1.0 - pj), 0.0)))
                cov[i][j] = cov[j][i] = c

        mu = []
        for i in range(n):
            e = _finite(edges[i], "edges[%d]" % i)
            mu.append(e if e > _finite(min_edge, "min_edge") else 0.0)
        try:
            solved = multivariate_kelly(mu, cov, lam)
            base = [max(v, 0.0) for v in solved]
            notes.append(
                "已使用多元 Kelly（含协方差）：ρ=%.2f 时 naive 逐注独立会高估 %.2f×"
                % (rho, naive_overbet_factor(rho))
            )
        except ValueError as exc:
            notes.append("多元 Kelly 求解失败，回退到逐注分数 Kelly：%s" % exc)
            used_cov = False

    # 2) 总敞口上限（按比例缩放，保持相对结构）
    total = math.fsum(base)
    penalty = naive_overbet_factor(rho) if rho > 0 else 1.0
    if total > max_total_exposure:
        scale = max_total_exposure / total
        base = [f * scale for f in base]
        total = math.fsum(base)
        notes.append(
            "总敞口 %.4f 超过上限 %.4f，已按比例缩放至上限"
            % (math.fsum(base) / scale if scale > 0 else total, max_total_exposure)
        )

    if rho <= 0:
        notes.append("未提供相关性（ρ=0）：报告 §8.5 提醒同场多市场 ρ 通常很高，"
                     "此处结果偏乐观")

    return SizingResult(
        fractions=tuple(base),
        outcomes=tuple(outcomes),
        lam=lam,
        used_covariance=used_cov,
        correlation_penalty=penalty,
        total_exposure=math.fsum(base),
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------- #
# §8.7 成本摊销
# --------------------------------------------------------------------------- #

def breakeven_bets(
    cost_per_day: float,
    ev: float,
    stake: float,
    q_fill: float = 1.0,
) -> float:
    """覆盖固定成本所需日均注数（报告 §8.7）。

        N = cost_day / (q_fill · EV · s)

    报告 §8.7 强调：每被拒一单，就需要额外多下若干单来覆盖固定成本——
    这是「死亡螺旋」的形式化。
    """
    cost_per_day = _finite(cost_per_day, "cost_per_day")
    ev = _finite(ev, "ev")
    stake = _finite(stake, "stake")
    q_fill = _prob(q_fill, "q_fill")

    if cost_per_day < 0:
        raise ValueError("日成本不能为负")
    if stake <= 0:
        raise ValueError("单注金额必须 > 0，实际 %r" % stake)
    denom = q_fill * ev * stake
    if denom <= 0:
        # 无正期望时，覆盖固定成本所需注数为无穷大（不可能完成）。
        # 用 math.inf 而非 float("inf")：前者是常量，语义更清晰。
        return math.inf
    return cost_per_day / denom


# --------------------------------------------------------------------------- #
# §8.6 几何增长率与风险
# --------------------------------------------------------------------------- #

def log_growth_rate(returns: Sequence[float]) -> float:
    """对数增长率 g = (1/T)·Σ log(1 + r_t)（报告 §8.6）。

    这是 Kelly 准则真正最大化的目标。注意 g 是**凹函数**：
    超过最优 f* 后，即使每注 EV 为正，g 仍会转负。
    """
    if not returns:
        raise ValueError("returns 不能为空")
    total = 0.0
    for i, r in enumerate(returns):
        r = _finite(r, "returns[%d]" % i)
        if r <= -1.0:
            raise ValueError(
                "单期收益 r 必须 > −1（否则本金归零），returns[%d]=%r" % (i, r)
            )
        total += math.log(1.0 + r)
    return total / len(returns)


def ruin_probability_bound(
    bankroll: float,
    stake: float,
    n_bets: Optional[float] = None,
) -> float:
    """破产概率的连续近似（报告 §8.6）。

    推导（Gambler's ruin 的连续形式）：
      设每注占本金比例 f = s/B0。经过 n 注后，
      输的路径使财富乘 (1−f)，赢的路径乘 (1+f)。
      假定 50/50 基准情形，相对财富的漂移为 ((1−f)/(1+f))^n，
      而破产阈值对应财富下降到 1−f 以下，需 (B0/s) 个「单位」的亏空，
      故指数为 B0/(f·s) 的等价形式 1/f²：

          P(ruin) ≈ ((1−f)/(1+f))^(1/f²)

    关键性质：**随 f 单调递增**——下注比例越大，破产概率越高。
    这正是报告 §8.6 想要的：f 超过最优值后，破产风险急剧上升。

    ⚠️ 这是**量级近似**，不是精确概率；仅用于比较不同 f 的相对风险。

    Args:
        bankroll: 当前本金 B0（> 0）。
        stake: 单注金额 s（≥ 0）。
        n_bets: 可选，保留参数以便后续细化（当前近似不使用）。

    Returns:
        必为 float：[0,1] 内的概率；f ≥ 1 时为 1.0。
        注意：本函数**不会返回 None**（早期版本标注为 Optional 属笔误）。
    """
    bankroll = _finite(bankroll, "bankroll")
    stake = _finite(stake, "stake")
    if bankroll <= 0:
        raise ValueError("本金必须 > 0")
    if stake < 0:
        raise ValueError("注额不能为负")
    if stake == 0:
        return 0.0

    f = stake / bankroll
    if f >= 1.0:
        return 1.0

    ratio = (1.0 - f) / (1.0 + f)
    if ratio <= 0.0:
        return 1.0
    # 指数 1/f²：f 越小，指数越大，(ratio < 1)^大数 → 接近 0
    exponent = 1.0 / (f * f)
    del n_bets  # 当前近似不使用注数，保留参数以便后续细化
    try:
        return min(max(ratio ** exponent, 0.0), 1.0)
    except OverflowError:
        return 0.0


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


def _normalize_tuple(p: Sequence[float]) -> Tuple[float, ...]:
    total = math.fsum(p)
    if total <= _EPS:
        n = len(p)
        return tuple(1.0 / n for _ in range(n)) if n else ()
    return tuple(x / total for x in p)


def _check_same_len(*seqs: Sequence[object]) -> None:
    if not seqs:
        return
    n = len(seqs[0])
    for s in seqs[1:]:
        if len(s) != n:
            raise ValueError("序列长度不一致：期望 %d，实际 %d" % (n, len(s)))
