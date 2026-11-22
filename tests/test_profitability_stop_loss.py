import json
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from infra.profitability.experiments.stop_loss import replay


def row(order, fixture, placed, net):
    placed = datetime.fromisoformat(placed).replace(tzinfo=timezone.utc)
    observed = placed + timedelta(minutes=1)
    payout = Decimal("10") + Decimal(str(net))
    return {
        "order_id": order, "fixture_id": fixture, "expert_id": "expert",
        "sport": "football", "market_kind": "handicap",
        "placed_at": placed.isoformat(), "signal_at": placed.isoformat(),
        "preflight_at": placed.isoformat(), "signal_price": "2.0",
        "preflight_price": "2.0", "overround": "0.03",
        "receipts": [{"net": str(net), "fees": None, "stake": "10",
                      "payout": str(payout), "currency": None,
                      "settled_at": observed.isoformat(),
                      "observed_at": observed.isoformat()}],
    }


class StopLossReplayTest(unittest.TestCase):
    def test_daily_loss_stop_uses_only_prior_visible_events(self):
        document = {
            "cutoff": "2026-11-04T00:00:00+00:00",
            "rows": [
                row("1", "f1", "2026-11-01T10:00:00", "-10"),
                row("2", "f2", "2026-11-01T10:02:00", "-10"),
                row("3", "f3", "2026-11-01T10:04:00", "10"),
            ],
        }
        result = {item["policy"]: item for item in replay(document)["policies"]}
        self.assertEqual(result["daily_loss_1"]["selected"], 1)
        self.assertEqual(result["daily_loss_1"]["blocked"], 2)
        self.assertFalse(result["daily_loss_1"]["uses_future_receipts"])

    def test_fixture_limit_and_fee_gate(self):
        document = {
            "cutoff": "2026-11-04T00:00:00+00:00",
            "rows": [
                row("1", "same", "2026-11-01T10:00:00", "-10"),
                row("2", "same", "2026-11-01T10:00:30", "10"),
            ],
        }
        result = {item["policy"]: item for item in replay(document)["policies"]}
        self.assertEqual(result["one_per_fixture"]["selected"], 1)
        self.assertEqual(result["one_per_fixture"]["blocked"], 1)
        self.assertEqual(result["verified_fee_currency_only"]["selected"], 0)
        self.assertEqual(result["verified_fee_currency_only"]["blocked"], 2)

    def test_output_marks_unknown_fees_without_zero_filling(self):
        document = {"cutoff": "2026-11-04T00:00:00+00:00",
                    "rows": [row("1", "f1", "2026-11-01T10:00:00", "-10")]}
        result = replay(document)
        self.assertEqual(result["policies"][0]["evidence_status"], "insufficient_fee_currency_evidence")
        self.assertEqual(result["policies"][0]["unknown_fee_or_currency_orders"], 1)

