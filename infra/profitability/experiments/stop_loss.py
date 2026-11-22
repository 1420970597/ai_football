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
    {"name": "verified_fee_currency_only", "kind": "evidence"},
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
    events = []
    for receipt in fill.receipts:
        if receipt.observed_at >= cutoff or receipt.unit_return is None:
            continue
        value = _stressed(receipt)
        if value is not None:
            events.append((receipt.observed_at, value))
    return sorted(events)


def _peak_drawdown(balance, peak):
    return max(ZERO, peak - balance)


def _policy_allows(policy, state, fixture, placed):
    kind = policy["kind"]
    if kind in {"fixture", "combined"}:
        held = state["open_by_fixture"].get(fixture, [])
        if len(held) >= policy["limit"] if kind == "fixture" else len(held) >= 1:
            return False
    if kind in {"daily_loss", "combined"}:
        if state["daily"][placed.date()] <= -policy["limit"]:
            return False
    if kind == "drawdown" and state["peak"] - state["balance"] >= policy["limit"]:
        return False
    if kind == "consecutive" and state["consecutive_losses"] >= policy["limit"]:
        return False
    if kind == "evidence":
        return False
    return True


def _release(state, current):
    for fixture, positions in state["open_by_fixture"].items():
        remaining = []
        for entry in positions:
            visible = next((at for at, _ in entry["events"] if at < current), None)
            if visible is None or visible >= current:
                remaining.append(entry)
        positions[:] = remaining


def _apply_events(state, events):
    for observed, value in events:
        if observed in state["seen_events"]:
            continue
        state["seen_events"].add(observed)
        state["balance"] += value
        state["peak"] = max(state["peak"], state["balance"])
        state["daily"][observed.date()] += value
        state["max_drawdown"] = max(state["max_drawdown"], _peak_drawdown(state["balance"], state["peak"]))
        if value < 0:
            state["consecutive_losses"] += 1
        elif value > 0:
            state["consecutive_losses"] = 0


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
            "seen_events": set(),
            "balance": ZERO,
            "peak": ZERO,
            "max_drawdown": ZERO,
            "consecutive_losses": 0,
            "selected": [],
            "blocked": 0,
            "blocked_reasons": defaultdict(int),
            "unknown_fee_currency": 0,
        }
    for fill in fills:
        for state in states.values():
            _release(state, fill.placed_at)
            events = []
            for selected in state["selected"]:
                events.extend((at, value) for at, value in selected["events"] if at < fill.placed_at)
            _apply_events(state, sorted(events))
            policy = state["policy"]
            if policy["kind"] == "evidence":
                state["blocked"] += 1
                state["blocked_reasons"]["missing_fee_currency_evidence"] += 1
                continue
            if not _policy_allows(policy, state, fill.fixture, fill.placed_at):
                state["blocked"] += 1
                state["blocked_reasons"][policy["kind"]] += 1
                continue
            receipt_events = _events(fill, cutoff)
            state["selected"].append({"fill": fill, "events": receipt_events})
            state["open_by_fixture"][fill.fixture].append({"events": receipt_events})
            if any(r.unit_fees is None or r.currency is None for r in fill.receipts):
                state["unknown_fee_currency"] += 1
    output = []
    for state in states.values():
        final_events = []
        for selected in state["selected"]:
            final_events.extend(selected["events"])
        _apply_events(state, sorted(final_events))
        settlements = len(final_events)
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
