"""Replay conservative stop-loss policies on the accepted native cohort.

Only receipts observed before the next placement can affect that placement.
Unknown fees and currencies remain an evidence gap. The reported stressed units
subtract the registered 0.02 profit haircut, but do not invent account fees.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
import argparse
import json
from pathlib import Path

from infra.profitability.simulate_strategies import Fill, Receipt, STRESS_COST


ZERO = Decimal("0")
POLICIES = (
    {"name": "baseline", "kind": "baseline"},
    {"name": "one_per_fixture", "kind": "fixture", "limit": 1},
    {"name": "two_per_fixture", "kind": "fixture", "limit": 2},
    {"name": "daily_loss_1", "kind": "daily_loss", "limit": Decimal("1")},
    {"name": "daily_loss_2", "kind": "daily_loss", "limit": Decimal("2")},
    {"name": "daily_loss_3", "kind": "daily_loss", "limit": Decimal("3")},
    {"name": "drawdown_2", "kind": "drawdown", "limit": Decimal("2")},
    {"name": "drawdown_3", "kind": "drawdown", "limit": Decimal("3")},
    {"name": "consecutive_losses_3", "kind": "consecutive", "limit": 3},
    {"name": "consecutive_losses_5", "kind": "consecutive", "limit": 5},
    {"name": "fixture_1_daily_loss_2", "kind": "combined", "limit": Decimal("2")},
    {"name": "strict_cost_evidence_gate", "kind": "evidence_gate"},
)


def _utc(value):
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.utcoffset() != timedelta(0):
        raise ValueError("UTC timestamp required")
    return moment


def _stressed(receipt):
    if receipt.unit_return is None:
        return None
    return receipt.unit_return - STRESS_COST


def _events(fill, cutoff):
    visible = [receipt for receipt in fill.receipts
               if receipt.settled_at is not None and receipt.unit_return is not None
               and receipt.observed_at < cutoff]
    if not visible:
        return None
    receipt = max(visible, key=lambda item: item.observed_at)
    value = _stressed(receipt)
    return (receipt.observed_at, value) if value is not None else None


def _peak_drawdown(balance, peak):
    return max(ZERO, peak - balance)


def _policy_allows(policy, state, fixture, placed):
    kind = policy["kind"]
    if kind in {"drawdown", "consecutive"} and state["latched"]:
        return False
    if kind in {"fixture", "combined"}:
        held = state["open_by_fixture"].get(fixture, [])
        if len(held) >= policy["limit"] if kind == "fixture" else len(held) >= 1:
            return False
    if kind in {"daily_loss", "combined"}:
        if placed.date() in state["daily_latched"] or state["daily"][placed.date()] <= -policy["limit"]:
            return False
    if kind == "drawdown" and state["peak"] - state["balance"] >= policy["limit"]:
        return False
    if kind == "consecutive" and state["consecutive_losses"] >= policy["limit"]:
        return False
    if kind == "evidence_gate":
        return False
    return True


def _release(state, current):
    for fixture, positions in state["open_by_fixture"].items():
        remaining = []
        for entry in positions:
            visible = _events(entry["fill"], current)
            if visible is None or visible[0] >= current:
                remaining.append(entry)
        positions[:] = remaining


def _rebuild_visible(state, current):
    state["balance"] = ZERO
    state["daily"] = defaultdict(lambda: ZERO)
    state["peak"] = ZERO
    state["consecutive_losses"] = 0
    events = []
    for selected in state["selected"]:
        visible = _events(selected["fill"], current)
        if visible is not None:
            events.append(visible)
    for observed, value in sorted(events):
        state["balance"] += value
        state["peak"] = max(state["peak"], state["balance"])
        state["daily"][observed.date()] += value
        state["max_drawdown"] = max(state["max_drawdown"], _peak_drawdown(state["balance"], state["peak"]))
        state["consecutive_losses"] = state["consecutive_losses"] + 1 if value < 0 else 0


def replay(document, policy_specs=POLICIES):
    if not isinstance(document, dict) or not isinstance(document.get("rows"), list):
        raise ValueError("cohort requires rows")
    cutoff = _utc(document["cutoff"])
    fills = sorted((Fill.parse(row) for row in document["rows"]), key=lambda f: (f.placed_at, f.order))
    states = {}
    for policy in policy_specs:
        states[policy["name"]] = {
            "policy": policy,
            "open_by_fixture": defaultdict(list),
            "daily": defaultdict(lambda: ZERO),
            "balance": ZERO,
            "peak": ZERO,
            "max_drawdown": ZERO,
            "consecutive_losses": 0,
            "latched": False,
            "daily_latched": set(),
            "selected": [],
            "blocked": 0,
            "blocked_reasons": defaultdict(int),
            "unknown_fee_currency": 0,
        }
    for fill in fills:
        for state in states.values():
            _rebuild_visible(state, fill.placed_at)
            _release(state, fill.placed_at)
            policy = state["policy"]
            if policy["kind"] == "evidence_gate":
                state["blocked"] += 1
                state["blocked_reasons"]["no_verified_cost_schedule"] += 1
                continue
            if not _policy_allows(policy, state, fill.fixture, fill.placed_at):
                state["blocked"] += 1
                state["blocked_reasons"][policy["kind"]] += 1
                if policy["kind"] in {"drawdown", "consecutive"}:
                    state["latched"] = True
                if policy["kind"] in {"daily_loss", "combined"}:
                    state["daily_latched"].add(fill.placed_at.date())
                continue
            state["selected"].append({"fill": fill})
            state["open_by_fixture"][fill.fixture].append({"fill": fill})
            latest = [r for r in fill.receipts
                      if r.settled_at is not None and r.unit_return is not None
                      and r.observed_at < cutoff]
            receipt = max(latest, key=lambda item: item.observed_at) if latest else None
            if receipt is not None and (receipt.unit_fees is None or receipt.currency is None):
                state["unknown_fee_currency"] += 1
    output = []
    for state in states.values():
        _rebuild_visible(state, cutoff)
        settlements = sum(_events(selected["fill"], cutoff) is not None for selected in state["selected"])
        output.append({
            "policy": state["policy"]["name"],
            "selected": len(state["selected"]),
            "blocked": state["blocked"],
            "settled_receipts": settlements,
            "stressed_units_before_verified_fees": state["balance"],
            "max_drawdown_units": state["max_drawdown"],
            "unknown_fee_or_currency_orders": state["unknown_fee_currency"],
            "blocked_reasons": dict(state["blocked_reasons"]),
            "evidence_status": "insufficient_fee_currency_evidence",
            "uses_future_receipts": False,
        })
    return {
        "study": "stop_loss_replay",
        "cutoff": cutoff.isoformat(),
        "rows": len(fills),
        "stress": "unit_return - 0.02 profit haircut; account fees remain unknown",
        "policies": output,
        "limits": [
            "accepted native cohort only; rejected opportunities are not estimable",
            "a pending receipt cannot trigger a stop before it is observed",
            "this is a loss containment replay, not proof of profitability",
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(json.loads(args.input.read_text())), default=str, sort_keys=True))


if __name__ == "__main__":
    main()
