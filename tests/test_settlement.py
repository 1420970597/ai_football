from decimal import Decimal
import unittest

from app.settlement import settle


class SettlementTests(unittest.TestCase):
    def signal(self, kind="total", selection="over", line="2.5", **extra):
        return {"kind": kind, "selection": selection, "line": line, "price": "1.90", "period": "full_time", "phase": "scheduled", **extra}

    def result(self, home=2, away=1, **extra):
        return {"status": "confirmed", "home": home, "away": away, **extra}

    def test_full_win_and_loss(self):
        value = settle(self.signal(), self.result())
        self.assertEqual(("win", Decimal("0.90")), (value.outcome, value.unit_net))
        self.assertEqual(Decimal("-1"), settle(self.signal(selection="under"), self.result()).unit_net)

    def test_quarter_total_half_win_and_half_loss(self):
        value = settle(self.signal(line="2.75"), self.result())
        self.assertEqual(("half_win", Decimal("0.45"), Decimal("0.5")), (value.outcome, value.unit_net, value.effective_stake))
        value = settle(self.signal(selection="under", line="2.75"), self.result())
        self.assertEqual(("half_loss", Decimal("-0.5")), (value.outcome, value.unit_net))

    def test_negative_quarter_handicap_is_split_correctly(self):
        value = settle(self.signal(kind="handicap", selection="home", line="-0.25"), self.result(home=1, away=1))
        self.assertEqual(("half_loss", Decimal("-0.5")), (value.outcome, value.unit_net))
        value = settle(self.signal(kind="handicap", selection="away", line="0.25"), self.result(home=1, away=1))
        self.assertEqual(("half_win", Decimal("0.45")), (value.outcome, value.unit_net))

    def test_push_void_and_pending(self):
        value = settle(self.signal(line="3"), self.result())
        self.assertEqual(("push", Decimal("0"), Decimal("0")), (value.outcome, value.unit_net, value.effective_stake))
        self.assertEqual("void", settle(self.signal(), {"status": "void"}).outcome)
        self.assertEqual("pending", settle(self.signal(), {"status": "pending"}).outcome)

    def test_three_way_draw_and_away_win(self):
        self.assertEqual("win", settle(self.signal("winner", "draw", "0"), self.result(1, 1)).outcome)
        self.assertEqual("win", settle(self.signal("winner", "away", "0"), self.result(0, 1)).outcome)
        self.assertEqual("loss", settle(self.signal("winner", "home", "0"), self.result(1, 1)).outcome)

    def test_first_half_and_second_half(self):
        result = self.result(3, 1, half_home=1, half_away=1)
        self.assertEqual("loss", settle(self.signal(period="first_half"), result).outcome)
        self.assertEqual("loss", settle(self.signal(period="second_half"), result).outcome)
        self.assertEqual("win", settle(self.signal(), result).outcome)

    def test_missing_half_score_does_not_become_zero(self):
        value = settle(self.signal(period="first_half"), self.result())
        self.assertEqual("pending", value.outcome)
        self.assertIsNone(value.unit_net)
        inconsistent = settle(self.signal(period='first_half'), self.result(1, 0, half_home=2, half_away=0))
        self.assertEqual('pending', inconsistent.outcome)

    def test_postponed_or_abandoned_is_not_implicitly_void(self):
        for state in ('cancelled','postponed','abandoned','half_time','extra_time'):
            with self.subTest(state=state):
                self.assertEqual('pending',settle(self.signal(),self.result(status=state)).outcome)

    def test_live_remaining_score_uses_snapshot_and_unknown_rules_stay_pending(self):
        signal = self.signal(kind="handicap", selection="home", line="0", phase="live")
        self.assertEqual("pending", settle(signal, self.result(2, 1)).outcome)
        signal.update(score_basis="remaining_period", score_baseline={"home": 1, "away": 0})
        self.assertEqual("push", settle(signal, self.result(2, 1)).outcome)
        signal["score_baseline"]["home"] = 99
        self.assertEqual("pending", settle(signal, self.result()).outcome)

    def test_invalid_or_unsupported_input_never_becomes_loss(self):
        for extra in ({"price": "NaN"}, {"price": "1"}, {"line": "2.1"}, {"kind": "correct_score"}, {"period": "extra_time"}, {"sport": "basketball"}):
            with self.subTest(extra=extra):
                self.assertEqual("pending", settle(self.signal(**extra), self.result()).outcome)
        for result in (self.result(-1, 2), self.result(True, 0), self.result(home="2")):
            self.assertEqual("pending", settle(self.signal(), result).outcome)


if __name__ == "__main__":
    unittest.main()
