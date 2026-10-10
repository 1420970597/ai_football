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


class BettingProtocolError(BettingBlocked):
    """The recommendation does not contain enough App order identifiers."""


VENUE_BET_PATH = "/yewu13/v1/betOrder/client/bet"
ENSEMBLE_ALGORITHM = "economic_ensemble"


def betting_capability(config: Any) -> dict[str, Any]:
    """Describe actual execution support independently of the saved gate.

    ``betting_enabled`` only controls plan generation in this build.  A
    restored true value cannot make the read-only adapter submit an order.
    """
    return {
        "configured_enabled": bool(getattr(config, "betting_enabled", False)),
        "execution_supported": False,
        "execution_enabled": False,
        "mode": "manual_only",
        "reason": "当前版本只提供投注计划预览和手工草稿，没有下单执行器；开启配置也不会提交订单。",
    }


def _pick_value(source: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = source.get(name)
        if value is not None and value != "":
            return value
    return None


def _required(source: Mapping[str, Any], *names: str) -> Any:
    value = _pick_value(source, *names)
    if value is None:
        raise BettingProtocolError("下注盘口缺少 App 字段：%s" % "/".join(names))
    return value


def _positive_number(value: Any, name: str) -> float:
    result = _number(value, name)
    if result <= 0:
        raise BettingProtocolError("%s 必须大于 0" % name)
    return result


def build_ybty_order_detail(pick: Mapping[str, Any], stake: float) -> dict[str, Any]:
    """Build one ``OBBetRequest.OBOrderDetail`` from App odds metadata.

    The live recommendation normally carries ``details[0]`` from the YBTY
    order DTO.  Keeping the aliases here also lets a caller pass the raw
    ``detailList`` item captured from the App.  No network request is made.
    """
    raw = pick.get("order_detail") or pick.get("detail")
    detail: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    details = pick.get("details")
    if not detail and isinstance(details, list) and details and isinstance(details[0], Mapping):
        detail = details[0]
    merged = dict(detail)
    merged.update({key: value for key, value in pick.items() if value is not None and value != ""})
    odd = _positive_number(_required(merged, "oddFinally", "odds", "odd"), "最终赔率")
    amount = _positive_number(stake, "投注金额")
    return {
        "betAmount": round(amount, 2),
        "matchId": str(_required(merged, "matchId", "match_id")),
        "marketId": str(_required(merged, "marketId", "market_id")),
        "playId": int(_required(merged, "playId", "play_id")),
        "playOptions": str(_required(merged, "playOptions", "play_options", "outcome")),
        "playOptionsId": str(_required(merged, "playOptionsId", "play_options_id", "optionId")),
        "oddFinally": str(odd),
        "marketValue": str(_pick_value(merged, "marketValue", "market_value", "line") or ""),
        "marketTypeFinally": str(_pick_value(merged, "marketTypeFinally", "marketType", "market_type") or "EU"),
        "matchType": int(_pick_value(merged, "matchType", "match_type") or 1),
        "sportId": int(_pick_value(merged, "sportId", "sport_id") or 1),
        "scoreBenchmark": str(_pick_value(merged, "scoreBenchmark", "score_benchmark") or ""),
    }


def build_ybty_bet_payload(pick: Mapping[str, Any], stake: float, *, device_imei: str = "") -> dict[str, Any]:
    """Build the Android App's single-bet request body without submitting it."""
    detail = build_ybty_order_detail(pick, stake)
    return {
        "acceptOdds": True,
        "currencyCode": "CNY",
        "deviceImei": str(device_imei),
        "deviceType": "android",
        "openMiltSingle": False,
        "preBet": False,
        "seriesOrders": [{
            "seriesType": "1",
            "seriesSum": 1,
            "fullBet": False,
            "orderDetailList": [detail],
        }],
    }


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
    # A provider order may only be produced from the persisted, aggregated
    # recommendation.  Individual algorithm forecasts do not represent the
    # portfolio decision and must never reach the order adapter.
    algorithm = str(pick.get("algorithm") or "")
    if algorithm != ENSEMBLE_ALGORITHM:
        raise BettingBlocked("只允许经济学综合推荐进入投注流程")
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
    execution = betting_capability(config)
    try:
        return {"allowed": True, "plan": plan_bet(pick, config, **kwargs).as_dict(),
                "submitted": False, "submission": "preview_only", "execution": execution}
    except BettingBlocked as exc:
        return {"allowed": False, "reason": str(exc), "plan": None,
                "submitted": False, "submission": "blocked", "execution": execution}


def draft_ybty_bet(pick: Mapping[str, Any], config: Any, **kwargs: Any) -> dict[str, Any]:
    """Create a reviewable App payload; never sends it to the venue.

    The returned ``payload`` matches the App's single-bet request shape, but
    the service deliberately does not expose a submit operation.  A human can
    compare it with the App confirmation screen and place the order there.
    """
    try:
        plan = plan_bet(pick, config, **kwargs)
        payload = build_ybty_bet_payload(pick, plan.stake)
        return {
            "allowed": True,
            "submitted": False,
            "execution": betting_capability(config),
            "submission": "manual_only",
            "requires_manual_confirmation": True,
            "provider_path": VENUE_BET_PATH,
            "plan": plan.as_dict(),
            "payload": payload,
        }
    except BettingBlocked as exc:
        return {"allowed": False, "submission": "blocked", "reason": str(exc), "plan": None,
                "submitted": False, "execution": betting_capability(config)}
