import json
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from infra.profitability.experiments.stop_loss import replay
from infra.profitability.simulate_strategies import Fill


CUTOFF = "2026-11-04T00:00:00+00:00"
BEGIN = "2026-11-01T00:00:00+00:00"


def instant(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def row(order, fixture, placed, net, *, observed=None, settled=None,
        fees=None, currency=None, signal_price="2.0", preflight_price="2.0"):
    placed_at = instant(placed)
    observed_at = instant(observed) if observed else placed_at + timedelta(minutes=1)
    settled_at = instant(settled) if settled else observed_at
    stake = Decimal("10")
    profit = Decimal(str(net))
    fee = Decimal(str(fees)) if fees is not None else None
    payout = stake + profit + (fee or Decimal("0"))
    return {
        "order_id": order,
        "fixture_id": fixture,
        "expert_id": "expert",
        "sport": "football",
        "market_kind": "handicap",
        "placed_at": placed_at.isoformat(),
        "signal_at": placed_at.isoformat(),
        "preflight_at": placed_at.isoformat(),
        "signal_price": signal_price,
        "preflight_price": preflight_price,
        "overround": "0.03",
        "receipts": [{
            "net": str(profit),
            "fees": str(fee) if fee is not None else None,
            "stake": str(stake),
            "payout": str(payout),
            "currency": currency,
            "settled_at": settled_at.isoformat() if settled is not False else None,
            "observed_at": observed_at.isoformat(),
        }],
    }


def document(rows, *, cutoff=CUTOFF, begin=BEGIN, end=None):
    return {
        "begin": begin,
        "end": end or cutoff,
        "cutoff": cutoff,
        "rows": rows,
    }


def policies_for(cohort):
    return {item["policy"]: item for item in replay(cohort)["policies"]}


class StopLossReplayTest(unittest.TestCase):
    def test_daily_loss_uses_only_prior_visible_settled_events(self):
        cohort = document([
            row("1", "f1", "2026-11-01T10:00:00", "-10"),
            row("2", "f2", "2026-11-01T10:02:00", "-10"),
            row("3", "f3", "2026-11-01T10:04:00", "10"),
        ])
        result = policies_for(cohort)
        self.assertEqual((result["daily_loss_1"]["selected"], result["daily_loss_1"]["blocked"]), (1, 2))

    def test_unsettled_receipt_does_not_trigger_stop_or_release_fixture(self):
        first = row("1", "same", "2026-11-01T10:00:00", "-10", settled=False)
        second = row("2", "same", "2026-11-01T10:00:30", "-10")
        third = row("3", "other", "2026-11-01T10:02:00", "-10")
        result = policies_for(document([first, second, third]))
        self.assertEqual((result["one_per_fixture"]["selected"], result["one_per_fixture"]["blocked"]), (2, 1))
        self.assertEqual(result["daily_loss_1"]["selected"], 2)
        self.assertEqual(result["baseline"]["settled_receipts"], 2)

    def test_latest_valid_revision_counts_once_and_rebuilds_drawdown(self):
        item = row("1", "f1", "2026-11-01T10:00:00", "-10",
                   observed="2026-11-01T10:01:00")
        item["receipts"].append({
            "net": "10", "fees": None, "stake": "10", "payout": "20", "currency": None,
            "settled_at": "2026-11-01T10:20:00+00:00",
            "observed_at": "2026-11-01T10:20:00+00:00",
        })
        later_wins = [
            row("2", "f2", "2026-11-01T10:21:00", "10"),
            row("3", "f3", "2026-11-01T10:23:00", "10"),
        ]
        result = policies_for(document([item, *later_wins]))["baseline"]
        self.assertEqual(result["settled_receipts"], 3)
        self.assertEqual(result["max_drawdown_units"], Decimal("0"))
        self.assertEqual(result["stressed_units_after_known_fees_before_unknown_fees"], Decimal("2.94"))

    def test_daily_stop_reopens_after_losing_revision_is_reversed(self):
        first = row("1", "f1", "2026-11-01T10:00:00", "-10",
                    observed="2026-11-01T10:01:00")
        blocked_entry = row("2", "f2", "2026-11-01T10:10:00", "10")
        first["receipts"].append({
            "net": "10", "fees": None, "stake": "10", "payout": "20", "currency": None,
            "settled_at": "2026-11-01T10:20:00+00:00",
            "observed_at": "2026-11-01T10:20:00+00:00",
        })
        after_correction = row("3", "f3", "2026-11-01T10:30:00", "10")
        result = policies_for(document([first, blocked_entry, after_correction]))["daily_loss_1"]
        self.assertEqual((result["selected"], result["blocked"]), (2, 1))

    def test_drawdown_stop_reopens_after_existing_positions_recover(self):
        rows = [
            row("1", "f1", "2026-11-01T10:00:00", "-10", observed="2026-11-01T10:05:00"),
            row("2", "f2", "2026-11-01T10:01:00", "-10", observed="2026-11-01T10:06:00"),
            row("3", "f3", "2026-11-01T10:02:00", "10", observed="2026-11-01T10:20:00"),
            row("4", "f4", "2026-11-01T10:03:00", "10", observed="2026-11-01T10:21:00"),
            row("5", "f5", "2026-11-01T10:04:00", "10", observed="2026-11-01T10:22:00"),
            row("6", "f6", "2026-11-01T10:10:00", "10"),
            row("7", "f7", "2026-11-01T10:30:00", "10"),
        ]
        result = policies_for(document(rows))["drawdown_2"]
        self.assertEqual((result["selected"], result["blocked"]), (6, 1))
        self.assertLessEqual(result["max_drawdown_units"], Decimal("2"))

    def test_same_timestamp_mixed_settlements_are_atomic_for_streak(self):
        rows = [
            row("1", "f1", "2026-11-01T10:00:00", "-10", observed="2026-11-01T10:20:00"),
            row("2", "f2", "2026-11-01T10:01:00", "-10", observed="2026-11-01T10:20:00"),
            row("3", "f3", "2026-11-01T10:02:00", "-10", observed="2026-11-01T10:20:00"),
            row("4", "f4", "2026-11-01T10:03:00", "10", observed="2026-11-01T10:20:00"),
            row("5", "f5", "2026-11-01T10:21:00", "10"),
        ]
        forward = policies_for(document(rows))["consecutive_losses_3"]
        reverse = policies_for(document(list(reversed(rows))) )["consecutive_losses_3"]
        self.assertEqual((forward["selected"], forward["blocked"]), (5, 0))
        self.assertEqual((forward["selected"], forward["blocked"]),
                         (reverse["selected"], reverse["blocked"]))

    def test_same_timestamp_loss_batch_drawdown_is_order_independent(self):
        rows = [
            row("1", "f1", "2026-11-01T10:00:00", "-10", observed="2026-11-01T10:20:00"),
            row("2", "f2", "2026-11-01T10:01:00", "-10", observed="2026-11-01T10:20:00"),
            row("3", "f3", "2026-11-01T10:02:00", "-10", observed="2026-11-01T10:20:00"),
            row("4", "f4", "2026-11-01T10:03:00", "10", observed="2026-11-01T10:20:00"),
        ]
        forward = policies_for(document(rows))["baseline"]
        reverse = policies_for(document(list(reversed(rows))))["baseline"]
        self.assertEqual(forward["max_drawdown_units"], Decimal("2.02"))
        self.assertEqual(forward["max_drawdown_units"], reverse["max_drawdown_units"])

    def test_orders_after_cutoff_are_excluded(self):
        after = row("2", "f2", "2026-11-04T00:01:00", "-10")
        cohort = document([
            row("1", "f1", "2026-11-03T23:00:00", "-10"),
            after,
        ])
        result = policies_for(cohort)["baseline"]
        self.assertEqual(result["selected"], 1)
        self.assertEqual(replay(cohort)["eligible_orders"], 1)
        self.assertEqual(replay(cohort)["excluded_outside_window"], 1)

    def test_combined_fixture_block_does_not_stop_other_fixtures(self):
        rows = [
            row("1", "same", "2026-11-01T10:00:00", "10"),
            row("2", "same", "2026-11-01T10:00:30", "10"),
            row("3", "other", "2026-11-01T10:03:00", "10"),
        ]
        result = policies_for(document(rows))["fixture_1_daily_loss_2"]
        self.assertEqual((result["selected"], result["blocked"]), (2, 1))
        self.assertEqual(result["blocked_reasons"], {"fixture_limit": 1})

    def test_daily_loss_budget_uses_utc_settlement_day(self):
        rows = [
            row("1", "f1", "2026-11-01T23:00:00", "-10", observed="2026-11-02T00:05:00"),
            row("2", "f2", "2026-11-01T23:10:00", "-10", observed="2026-11-02T00:06:00"),
            row("3", "f3", "2026-11-02T00:08:00", "-10"),
            row("4", "f4", "2026-11-02T00:10:00", "-10"),
        ]
        result = policies_for(document(rows))["daily_loss_2"]
        self.assertEqual((result["selected"], result["blocked"]), (2, 2))
        self.assertEqual(replay(document(rows))["daily_loss_basis"], "UTC settlement-observation date")

    def test_verified_fee_is_deducted_and_push_remains_zero_before_fee(self):
        winning = row("1", "f1", "2026-11-01T10:00:00", "9.8",
                      fees="0.2", currency="CNY")
        pushing = row("2", "f2", "2026-11-01T10:02:00", "0")
        result = policies_for(document([winning, pushing]))["baseline"]
        self.assertEqual(result["stressed_units_after_known_fees_before_unknown_fees"], Decimal("0.96"))
        self.assertEqual(result["settled_orders_missing_fee_or_currency_at_cutoff"], 1)

    def test_pushes_do_not_form_a_losing_streak(self):
        pushes = [
            row("1", "f1", "2026-11-01T10:00:00", "0"),
            row("2", "f2", "2026-11-01T10:02:00", "0"),
            row("3", "f3", "2026-11-01T10:04:00", "0"),
            row("4", "f4", "2026-11-01T10:06:00", "0"),
        ]
        result = policies_for(document(pushes))["consecutive_losses_3"]
        self.assertEqual((result["selected"], result["blocked"]), (4, 0))
        self.assertEqual(result["stressed_units_after_known_fees_before_unknown_fees"], Decimal("0"))

    def test_duplicate_order_ids_are_rejected(self):
        repeated = row("same", "f2", "2026-11-01T10:01:00", "10")
        with self.assertRaisesRegex(ValueError, "duplicate order_id"):
            replay(document([row("same", "f1", "2026-11-01T10:00:00", "-10"), repeated]))

    def test_stop_loss_and_simulator_share_latest_valid_receipt(self):
        item = row("1", "f1", "2026-11-01T10:00:00", "-10")
        item["receipts"].append({
            "net": "3", "fees": None, "stake": "10", "payout": "20", "currency": None,
            "settled_at": "2026-11-01T10:02:00+00:00",
            "observed_at": "2026-11-01T10:02:00+00:00",
        })
        fill = Fill.parse(item)
        latest = fill.latest_receipt(datetime.fromisoformat(CUTOFF))
        self.assertIsNotNone(latest)
        self.assertEqual(latest.unit_return, Decimal("-1"))
        result = policies_for(document([item]))["baseline"]
        self.assertEqual(result["settled_receipts"], 1)
        self.assertEqual(result["stressed_units_after_known_fees_before_unknown_fees"], Decimal("-1"))

    def test_conflicting_receipt_versions_with_identical_time_are_ambiguous(self):
        item = row("1", "f1", "2026-11-01T10:00:00", "-10",
                   observed="2026-11-01T10:02:00")
        item["receipts"].append({
            "net": "10", "fees": None, "stake": "10", "payout": "20", "currency": None,
            "settled_at": "2026-11-01T10:02:00+00:00",
            "observed_at": "2026-11-01T10:02:00+00:00",
        })
        fill = Fill.parse(item)
        self.assertIsNone(fill.latest_receipt(datetime.fromisoformat(CUTOFF)))
        result = policies_for(document([item]))["baseline"]
        self.assertEqual(result["settled_receipts"], 0)

    def test_malformed_rows_raise_value_error(self):
        malformed = {"order_id": "1", "fixture_id": "f1"}
        with self.assertRaisesRegex(ValueError, "malformed order row"):
            replay(document([malformed]))

    def test_invalid_or_missing_interval_is_rejected(self):
        cohort = document([row("1", "f1", "2026-11-01T10:00:00", "10")])
        cohort["end"] = "2026-10-31T23:59:00+00:00"
        with self.assertRaisesRegex(ValueError, "cohort interval"):
            replay(cohort)
        missing = {"rows": [], "cutoff": CUTOFF}
        with self.assertRaisesRegex(ValueError, "begin, end and cutoff"):
            replay(missing)

    def test_native_price_quality_gate_is_explicit(self):
        good = row("1", "f1", "2026-11-01T10:00:00", "-10")
        bad = row("2", "f2", "2026-11-01T10:02:00", "10", preflight_price="1.9")
        result = policies_for(document([good, bad]))["native_price_quality_only"]
        self.assertEqual((result["selected"], result["blocked"]), (1, 1))
        self.assertEqual(result["blocked_reasons"], {"native_price_quality": 1})

    def test_unknown_fees_do_not_use_a_future_revision_at_decision(self):
        item = row("1", "f1", "2026-11-01T10:00:00", "-5",
                   observed="2026-11-01T10:01:00")
        later_fee_revision = {
            "net": "-11", "fees": "6", "stake": "10", "payout": "5", "currency": "CNY",
            "settled_at": "2026-11-01T10:01:00+00:00",
            "observed_at": "2026-11-01T10:08:00+00:00",
        }
        item["receipts"].append(later_fee_revision)
        second = row("2", "f2", "2026-11-01T10:05:00", "0")
        third = row("3", "f3", "2026-11-01T10:10:00", "10")
        result = policies_for(document([item, second, third]))
        self.assertEqual((result["daily_loss_1"]["selected"], result["daily_loss_1"]["blocked"]), (2, 1))
        self.assertEqual(result["daily_loss_1"]["settled_orders_missing_fee_or_currency_at_cutoff"], 1)

    def test_pending_future_settlement_does_not_change_the_earlier_decision(self):
        first = row("1", "f1", "2026-11-01T10:00:00", "-10",
                    observed="2026-11-01T10:01:00")
        second = row("2", "f2", "2026-11-01T10:00:30", "-10")
        result = policies_for(document([first, second]))["daily_loss_1"]
        self.assertEqual((result["selected"], result["blocked"]), (2, 0))

    def test_simulator_excludes_unsettled_receipt_from_realized_outcomes(self):
        pending = row("1", "f1", "2026-11-01T10:00:00", "-10", settled=False)
        fill = Fill.parse(pending)
        self.assertIsNone(fill.latest_receipt(datetime.fromisoformat(CUTOFF)))

    def test_unsettled_revision_does_not_replace_latest_settled_outcome(self):
        item = row("1", "f1", "2026-11-01T10:00:00", "10",
                   observed="2026-11-01T10:05:00")
        item["receipts"].append({
            "net": "-10", "fees": None, "stake": "10", "payout": "0", "currency": None,
            "settled_at": None,
            "observed_at": "2026-11-01T10:10:00+00:00",
        })
        fill = Fill.parse(item)
        latest = fill.latest_receipt(datetime.fromisoformat(CUTOFF))
        self.assertIsNotNone(latest)
        self.assertEqual(latest.unit_return, Decimal("1"))
