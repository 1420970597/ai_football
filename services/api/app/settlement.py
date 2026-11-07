"""足球专家一单位结算。输入证据不足时返回 pending，禁止猜测滚球规则。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

ZERO = Decimal("0")
ONE = Decimal("1")
HALF = Decimal("0.5")


@dataclass(frozen=True)
class Settlement:
    outcome: str
    unit_net: Decimal | None
    effective_stake: Decimal
    reason: str


def pending(reason: str) -> Settlement:
    return Settlement("pending", None, ZERO, reason)


def _score(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _period_scores(period: str, result: dict) -> tuple[int, int] | None:
    for key in ('home', 'away'):
        full, half = _score(result.get(key)), _score(result.get('half_' + key))
        if full is not None and half is not None and half > full:
            return None
    if period == "first_half":
        home, away = _score(result.get("half_home")), _score(result.get("half_away"))
    elif period in {"full_time", "second_half"}:
        home, away = _score(result.get("home")), _score(result.get("away"))
        if period == "second_half":
            first_home, first_away = _score(result.get("half_home")), _score(result.get("half_away"))
            if None in (home, away, first_home, first_away):
                return None
            home, away = home - first_home, away - first_away
    else:
        return None
    return None if home is None or away is None or home < 0 or away < 0 else (home, away)


def settle(signal: dict, result: dict) -> Settlement:
    if signal.get("sport", "football") != "football":
        return pending("sport_requires_provider_settlement")
    if result.get("status") == "void":
        return Settlement("void", ZERO, ZERO, "provider_confirmed_void")
    if result.get("status") != "confirmed":
        return pending("result_not_confirmed")
    try:
        price = Decimal(str(signal["price"]))
        kind = signal.get('kind')
        raw_line = signal.get('line')
        if isinstance(raw_line, bool):
            return pending("invalid_price_or_line")
        line = None if kind == 'winner' and raw_line is None else Decimal(str(raw_line))
        if (not price.is_finite() or price <= ONE or
                kind == 'winner' and line not in (None, ZERO) or
                kind != 'winner' and (line is None or not line.is_finite())):
            return pending("invalid_price_or_line")
    except (KeyError, InvalidOperation, ValueError):
        return pending("invalid_price_or_line")
    scores = _period_scores(signal.get("period", "full_time"), result)
    if scores is None:
        return pending("period_score_missing_or_inconsistent")
    basis = signal.get("score_basis", "unknown")
    zero_equivalent = False
    if signal.get("phase") == "scheduled" and basis == "unknown":
        basis = "full_period"
    if basis == "remaining_period":
        baseline = _period_scores(signal.get("period", "full_time"), signal.get("score_baseline") or {})
        if baseline is None:
            return pending("live_score_baseline_missing")
        scores = scores[0] - baseline[0], scores[1] - baseline[1]
        if min(scores) < 0:
            return pending("baseline_exceeds_official_result")
    elif basis != "full_period":
        # Explicit zero in this period makes full and remaining scores equal.
        # Keep the original unknown rule; missing period scores are not zero.
        baseline = _period_scores(signal.get("period", "full_time"), signal.get("score_baseline") or {})
        if signal.get("phase") != "live" or basis != "unknown" or baseline is None:
            return pending("live_market_score_rule_unknown")
        if baseline == (0, 0):
            zero_equivalent = True
        else:
            # Do not choose a provider rule or rewrite the immutable unknown
            # basis. A result is provable only when BOTH supported meanings
            # yield the same outcome, net return and effective stake.
            variants = [settle(dict(signal, score_basis=explicit), result)
                        for explicit in ('full_period', 'remaining_period')]
            full, remaining = variants
            if (full.outcome != 'pending' and remaining.outcome != 'pending'
                and (full.outcome, full.unit_net, full.effective_stake)
                == (remaining.outcome, remaining.unit_net, remaining.effective_stake)):
                return Settlement(full.outcome, full.unit_net, full.effective_stake,
                                  full.reason + ':score_basis_outcome_equivalent')
            return pending("live_market_score_rule_unknown")
    reason_suffix = ":zero_baseline_score_equivalent" if zero_equivalent else ""
    home, away = map(Decimal, scores)
    kind, selection = signal.get("kind"), signal.get("selection")
    line_basis = signal.get('line_basis', 'selection')
    if line_basis not in {'selection', 'provider_home'}:
        return pending('line_basis_unknown')
    if line_basis == 'provider_home':
        if kind != 'handicap' or selection not in {'home', 'away'}:
            return pending('line_basis_not_applicable')
        if selection == 'away':
            line = -line
        reason_suffix += ':provider_home_line'
    if kind == "winner":
        if selection not in {"home", "away", "draw"}:
            return pending("unsupported_selection")
        won = (home > away and selection == "home") or (away > home and selection == "away") or (home == away and selection == "draw")
        return Settlement("win" if won else "loss", price - ONE if won else -ONE, ONE, "three_way_winner" + reason_suffix)
    if kind not in {"handicap", "total", "team_total"}:
        return pending("unsupported_market")
    team_side = signal.get('team_side')
    if kind == 'team_total' and team_side not in {'home', 'away'}:
        return pending('team_total_side_unknown')
    if (line * 4) != (line * 4).to_integral_value():
        return pending("unsupported_line_increment")
    if kind == "handicap" and selection not in {"home", "away"} or kind in {"total", "team_total"} and selection not in {"over", "under"}:
        return pending("unsupported_selection")
    if kind in {"total", "team_total"} and line < 0:
        return pending("invalid_total_line")
    # line 是所选方的让受让，不能把客队线再次反号。
    lower = (line / HALF).to_integral_value(rounding=ROUND_FLOOR) * HALF
    lines = [line] if lower == line else [lower, lower + HALF]
    returns = []
    for part in lines:
        if kind == "handicap":
            margin = (home - away if selection == "home" else away - home) + part
        else:
            goals = home if team_side == 'home' else away
            margin = (goals if kind == 'team_total' else home + away) - part
            if selection == "under":
                margin = -margin
        returns.append(price - ONE if margin > 0 else -ONE if margin < 0 else ZERO)
    net = sum(returns, ZERO) / len(returns)
    active = Decimal(sum(value != ZERO for value in returns)) / len(returns)
    if active == 0:
        outcome = "push"
    elif active == HALF:
        outcome = "half_win" if net > 0 else "half_loss"
    else:
        outcome = "win" if net > 0 else "loss"
    reason = "asian_split" if len(lines) == 2 else "asian_line"
    return Settlement(outcome, net, active, reason + reason_suffix)
