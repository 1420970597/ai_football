#!/usr/bin/env python3
"""Guarded betting plan generation.

This module deliberately stops at a validated order plan.  Provider payloads
are venue-specific and must be implemented by an explicitly configured
adapter; importing or testing this module never sends a real-money request.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


class BettingBlocked(ValueError):
    """The configured safety gate rejected an order plan."""


@dataclass(frozen=True)
class BetPlan:
    match_id: str
    market: str
    line: str
    outcome: str
    stake: float
    confidence: float
    hit_count: float
    mode: str
    executable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id, "market": self.market,
            "line": self.line, "outcome": self.outcome,
            "stake": self.stake, "confidence": self.confidence,
            "hit_count": self.hit_count, "mode": self.mode,
            "executable": self.executable,
        }


def _number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BettingBlocked("%s 不是有效数值" % name) from exc
    if not math.isfinite(result):
        raise BettingBlocked("%s 必须是有限数值" % name)
    return result


def plan_bet(pick: Mapping[str, Any], config: Any,
             *, match_live: bool = True,
             market_open: bool = True) -> BetPlan:
    """Validate a recommendation and calculate its configured stake.

    ``pick`` must contain the ensemble recommendation's confidence and its
    historical hit count.  Hit count is intentionally checked before stake
    calculation, so a high confidence with no settled evidence cannot result
    in an order plan.
    """
    if not bool(getattr(config, "betting_enabled", False)):
        raise BettingBlocked("投注功能未启用")
    if not match_live:
        raise BettingBlocked("比赛不在进行中")
    if not market_open:
        raise BettingBlocked("盘口已关闭或暂停")
    hit_count = _number(pick.get("hit_count", pick.get("historical_hit_count", 0)), "盘口命中次数")
    minimum = _number(getattr(config, "betting_min_hit_count", 3), "最小命中次数")
    if hit_count < minimum:
        raise BettingBlocked("盘口历史命中次数不足（%.0f/%g）" % (hit_count, minimum))
    confidence = min(1.0, max(0.0, _number(
        pick.get("composite_confidence", pick.get("confidence", 0)), "综合置信度")))
    base = _number(getattr(config, "betting_fixed_stake", 10.0), "固定额度")
    mode = str(getattr(config, "betting_stake_mode", "fixed"))
    if mode == "fixed":
        stake = base
    elif mode == "confidence_multiplier":
        stake = base * confidence
    else:
        raise BettingBlocked("未知投注额度模式")
    if stake <= 0:
        raise BettingBlocked("计算出的投注额度必须大于 0")
    match_id = str(pick.get("match_id") or "")
    market = str(pick.get("market") or "")
    outcome = str(pick.get("outcome") or "")
    if not match_id or not market or not outcome:
        raise BettingBlocked("盘口缺少比赛、玩法或选项标识")
    return BetPlan(match_id, market, str(pick.get("line") or ""), outcome,
                   round(stake, 2), round(confidence, 6), hit_count, mode)


def preview_bet(pick: Mapping[str, Any], config: Any, **kwargs: Any) -> dict[str, Any]:
    """Return a stable UI/API response without submitting an order."""
    try:
        return {"allowed": True, "plan": plan_bet(pick, config, **kwargs).as_dict()}
    except BettingBlocked as exc:
        return {"allowed": False, "reason": str(exc), "plan": None}
