"""Replay conservative stop-loss policies on the accepted native cohort.

Decisions use only valid receipts observed before each placement. The daily loss
budget uses the UTC settlement-observation day. Simultaneous settlements are
applied as one batch so receipt ordering cannot change the stop result.
Unknown fees and currency remain visible. The fixed 0.02 stress cost applies
to non-push returns, matching the registered paper evaluator and the one-unit
push rule.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import groupby
import argparse
import json
from pathlib import Path

from infra.profitability.simulate_strategies import Fill


ZERO = Decimal("0")
POLICIES = (
    {"name": "baseline", "kind": "baseline"},
    {"name": "one_per_fixture", "kind": "fixture", "fixture_limit": 1},
    {"name": "two_per_fixture", "kind": "fixture", "fixture_limit": 2},
    {"name": "daily_loss_1", "kind": "daily_loss", "daily_loss_limit": Decimal("1")},
    {"name": "daily_loss_2", "kind": "daily_loss", "daily_loss_limit": Decimal("2")},
    {"name": "daily_loss_3", "kind": "daily_loss", "daily_loss_limit": Decimal("3")},
    {"name": "drawdown_2", "kind": "drawdown", "drawdown_limit": Decimal("2")},
    {"name": "drawdown_3", "kind": "drawdown", "drawdown_limit": Decimal("3")},
    {"name": "consecutive_losses_3", "kind": "consecutive", "loss_limit": 3},
    {"name": "consecutive_losses_5", "kind": "consecutive", "loss_limit": 5},
    {"name": "fixture_1_daily_loss_2", "kind": "combined",
     "fixture_limit": 1, "daily_loss_limit": Decimal("2")},
    {"name": "native_price_quality_only", "kind": "price_quality"},
    {"name": "price_quality_fixture_1_daily_loss_2", "kind": "quality_combined",
     "fixture_limit": 1, "daily_loss_limit": Decimal("2")},
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
    return receipt.stressed_return


def _visible_settlement(fill, cutoff):
    receipt = fill.latest_receipt(cutoff)
    if receipt is None or receipt.settled_at is None:
        return None
    value = _stressed(receipt)
    if value is None:
        return None
    return receipt.observed_at, value, fill.placed_at.date(), fill.fixture, fill.order


def _policy_block_reason(policy, state, fill):
    kind = policy["kind"]
    if kind == "evidence_gate":
        return "no_verified_cost_schedule"
    if kind in {"price_quality", "quality_combined"} and not fill.price_quality:
        return "native_price_quality"
    if kind in {"fixture", "combined", "quality_combined"}:
        positions = state["open_by_fixture"].get(fill.fixture, ())
        if len(positions) >= policy["fixture_limit"]:
            return "fixture_limit"
    if kind in {"daily_loss", "combined", "quality_combined"}:
        if state["daily"][fill.placed_at.date()] <= -policy["daily_loss_limit"]:
            return "daily_loss"
    if kind == "drawdown" and state["peak"] - state["balance"] >= policy["drawdown_limit"]:
        return "drawdown"
    if kind == "consecutive" and state["consecutive_losses"] >= policy["loss_limit"]:
        return "consecutive_losses"
    return None


def _release(state, current):
    for fixture, positions in state["open_by_fixture"].items():
        positions[:] = [
            entry for entry in positions
            if _visible_settlement(entry["fill"], current) is None
        ]


def _rebuild_visible(state, current):
    state["daily"] = defaultdict(lambda: ZERO)
    state["balance"] = ZERO
    state["peak"] = ZERO
    state["max_drawdown"] = ZERO
    state["consecutive_losses"] = 0

    events = []
    for selected in state["selected"]:
        event = _visible_settlement(selected["fill"], current)
        if event is not None:
            events.append(event)

    events.sort(key=lambda event: (event[0], event[2], event[3], event[4]))
    for _, timestamp_group in groupby(events, key=lambda event: event[0]):
        batch = list(timestamp_group)
        batch_return = sum((event[1] for event in batch), ZERO)
        state["balance"] += batch_return
        state["peak"] = max(state["peak"], state["balance"])
        state["max_drawdown"] = max(
            state["max_drawdown"],
            max(ZERO, state["peak"] - state["balance"]),
        )
        settlement_day = batch[0][0].date()
        state["daily"][settlement_day] += batch_return
        if batch and all(event[1] < ZERO for event in batch):
            state["consecutive_losses"] += len(batch)
        else:
            state["consecutive_losses"] = 0


def replay(document, policy_specs=POLICIES):
    if not isinstance(document, dict) or not isinstance(document.get("rows"), list):
        raise ValueError("cohort requires a rows array")
    try:
        begin = _utc(document["begin"])
        end = _utc(document["end"])
        cutoff = _utc(document["cutoff"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("cohort requires begin, end and cutoff UTC timestamps") from error
    if not begin < end <= cutoff:
        raise ValueError("cohort interval must be ordered and no later than cutoff")

    order_ids = []
    fills = []
    for row in document["rows"]:
        if not isinstance(row, dict):
            raise ValueError("cohort rows must be objects")
        try:
            order_id = row["order_id"]
            fill = Fill.parse(row)
        except (KeyError, TypeError, ValueError, ArithmeticError) as error:
            raise ValueError("cohort contains a malformed order row") from error
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("cohort order_id must be a non-empty string")
        order_ids.append(order_id)
        fills.append(fill)
    if len(order_ids) != len(set(order_ids)):
        raise ValueError("duplicate order_id in cohort")

    eligible = [
        fill for fill in fills
        if begin <= fill.placed_at < end and fill.placed_at < cutoff
    ]
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
            "selected": [],
            "blocked": 0,
            "blocked_reasons": defaultdict(int),
        }

    for fill in sorted(eligible, key=lambda item: (item.placed_at, item.order)):
        for state in states.values():
            _rebuild_visible(state, fill.placed_at)
            _release(state, fill.placed_at)
            reason = _policy_block_reason(state["policy"], state, fill)
            if reason is not None:
                state["blocked"] += 1
                state["blocked_reasons"][reason] += 1
                continue
            state["selected"].append({"fill": fill})
            state["open_by_fixture"][fill.fixture].append({"fill": fill})

    output = []
    for state in states.values():
        _rebuild_visible(state, cutoff)
        latest_settlements = [
            _visible_settlement(entry["fill"], cutoff)
            for entry in state["selected"]
        ]
        latest_settlements = [event for event in latest_settlements if event is not None]
        unknown_fees = 0
        for entry in state["selected"]:
            receipt = entry["fill"].latest_receipt(cutoff)
            if receipt is not None and receipt.settled_at is not None:
                unknown_fees += int(receipt.unit_fees is None or receipt.currency is None)

        output.append({
            "policy": state["policy"]["name"],
            "selected": len(state["selected"]),
            "blocked": state["blocked"],
            "settled_receipts": len(latest_settlements),
            "stressed_units_after_known_fees_before_unknown_fees": state["balance"],
            "max_drawdown_units": state["max_drawdown"],
            "settled_orders_missing_fee_or_currency_at_cutoff": unknown_fees,
            "blocked_reasons": dict(state["blocked_reasons"]),
            "evidence_status": "insufficient_fee_currency_evidence",
        })

    return {
        "study": "stop_loss_replay",
        "window": {"begin": begin.isoformat(), "end": end.isoformat(), "cutoff": cutoff.isoformat()},
        "rows_in_archive": len(fills),
        "eligible_orders": len(eligible),
        "excluded_outside_window": len(fills) - len(eligible),
        "stress": "fixed 0.02 per-unit cost on non-push returns; subtract fees only when fee and currency are known",
        "daily_loss_basis": "UTC settlement-observation date",
        "simultaneous_settlement_rule": "apply each timestamp group as one net batch; mixed batch resets consecutive-loss count",
        "policies": output,
        "limits": [
            "accepted native cohort only; rejected opportunities are not estimable",
            "a pending, invalid, or future receipt cannot trigger a stop or release fixture exposure",
            "the replay is loss containment analysis, not proof of profitability",
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(json.loads(args.input.read_text())), default=str, sort_keys=True))


if __name__ == "__main__":
    main()
