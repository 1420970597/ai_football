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

    def test_zero_period_baseline_matches_both_explicit_rules(self):
        baseline = dict(home=0, away=0, half_home=0, half_away=0)
        markets = [('total', side, line) for side in ('over', 'under') for line in ('0', '.75', '2', '2.75')]
        markets += [('handicap', side, line) for side in ('home', 'away') for line in ('-.25', '0', '.25', '.75')]
        markets += [('winner', side, None) for side in ('home', 'away', 'draw')]
        for period in ('full_time', 'first_half', 'second_half'):
            for kind, selection, line in markets:
                for home, away in ((0, 0), (1, 0), (1, 1), (3, 1)):
                    with self.subTest(period=period, kind=kind, selection=selection, line=line, score=(home, away)):
                        signal = self.signal(kind, selection, line, phase='live', period=period, score_baseline=baseline)
                        result = self.result(home, away, half_home=0, half_away=0)
                        value = settle(signal, result)
                        self.assertNotEqual('pending', value.outcome)
                        self.assertTrue(value.reason.endswith(':zero_baseline_score_equivalent'))
                        for basis in ('full_period', 'remaining_period'):
                            explicit = settle(dict(signal, score_basis=basis), result)
                            self.assertEqual((explicit.outcome, explicit.unit_net, explicit.effective_stake),
                                             (value.outcome, value.unit_net, value.effective_stake))
                        self.assertNotIn('score_basis', signal)

    def test_zero_baseline_requires_explicit_valid_selected_period_scores(self):
        result = self.result(2, 1, half_home=0, half_away=0)
        for period, baseline in (
            ('full_time', {}), ('full_time', {'home': 1, 'away': 0}),
            ('full_time', {'home': True, 'away': 0}), ('full_time', {'home': -1, 'away': 0}),
            ('full_time', {'home': 0, 'away': 0, 'half_home': 1}),
            ('first_half', {'home': 0, 'away': 0}),
            ('first_half', {'half_home': '0', 'half_away': 0}),
            ('second_half', {'home': 0, 'away': 0}),
        ):
            with self.subTest(period=period, baseline=baseline):
                value = settle(self.signal(phase='live', period=period, score_baseline=baseline), result)
                self.assertEqual('pending', value.outcome)
                self.assertIsNone(value.unit_net)
        zero = dict(home=0, away=0, half_home=0, half_away=0)
        for extra in ({'score_basis': 'unrecognised'}, {'sport': 'basketball'}, {'price': 'NaN'},
                      {'period': 'extra_time'}, {'line': '0.1'}, {'selection': 'invalid'}, {'kind': 'team_total'}):
            with self.subTest(extra=extra):
                self.assertEqual('pending', settle(self.signal(phase='live', score_baseline=zero, **extra), result).outcome)
        self.assertEqual('pending', settle(self.signal(phase='live', score_baseline=zero), self.result(status='provisional')).outcome)

    def test_nonzero_baseline_only_settles_identical_explicit_financial_results(self):
        baseline = dict(home=1, away=0, half_home=1, half_away=0)
        markets = [('total', side, line) for side in ('over', 'under')
                   for line in ('0', '.75', '1', '1.25', '1.75', '2', '2.25', '2.75', '4')]
        markets += [('handicap', side, line) for side in ('home', 'away')
                    for line in ('-2.25', '-.75', '-.25', '0', '.25', '.75', '2.25')]
        markets += [('winner', side, None) for side in ('home', 'away', 'draw')]
        agreed = set()
        conflicts = 0
        for period in ('full_time', 'first_half', 'second_half'):
            selected_baseline = baseline if period != 'second_half' else dict(
                home=2, away=0, half_home=1, half_away=0)
            for kind, selection, line in markets:
                for home, away in ((1, 0), (1, 1), (2, 0), (3, 1)):
                    signal = self.signal(kind, selection, line, phase='live', period=period,
                                         score_baseline=selected_baseline)
                    result = self.result(home, away, half_home=1, half_away=0)
                    full, remaining = [settle(dict(signal, score_basis=b), result)
                                       for b in ('full_period', 'remaining_period')]
                    actual = settle(signal, result)
                    compare = lambda s: (s.outcome, s.unit_net, s.effective_stake)
                    if full.outcome != 'pending' and remaining.outcome != 'pending' and compare(full) == compare(remaining):
                        self.assertEqual(compare(full), compare(actual))
                        self.assertTrue(actual.reason.endswith(':score_basis_outcome_equivalent'))
                        agreed.add(actual.outcome)
                    else:
                        self.assertEqual('pending', actual.outcome)
                        self.assertIsNone(actual.unit_net)
                        conflicts += 1
                    self.assertNotIn('score_basis', signal)
        for line, expected in (('.25', 'half_win'), ('-.25', 'half_loss'), ('0', 'push')):
            value = settle(self.signal('handicap', 'home', line, phase='live',
                           score_baseline={'home':1,'away':1}), self.result(2, 2))
            self.assertEqual(expected, value.outcome)
            self.assertTrue(value.reason.endswith(':score_basis_outcome_equivalent'))
            agreed.add(value.outcome)
        self.assertTrue({'win', 'loss', 'half_win', 'half_loss', 'push'} <= agreed)
        self.assertGreater(conflicts, 0)

    def test_nonzero_equivalence_does_not_ignore_invalid_evidence_or_distinct_rule(self):
        result = self.result(1, 0, half_home=1, half_away=0)
        baseline = dict(home=1, away=0, half_home=1, half_away=0)
        good = self.signal(line='1.75', phase='live', score_baseline=baseline)
        actual = settle(good, result)
        self.assertEqual(('loss', Decimal('-1'), Decimal('1')),
                         (actual.outcome, actual.unit_net, actual.effective_stake))
        for extra in ({'score_basis':'other'}, {'score_baseline':{}},
                      {'score_baseline':{'home':2,'away':0}}, {'period':'first_half','score_baseline':{'home':1,'away':0}},
                      {'price':'NaN'}, {'line':'.1'}, {'selection':'bad'}, {'kind':'team_total'}):
            self.assertEqual('pending', settle(dict(good, **extra), result).outcome)
        self.assertEqual('pending', settle(good, dict(result,status='provisional')).outcome)

    def test_actual_zero_baseline_half_over_is_loss(self):
        value = settle(self.signal(line='.75', price='1.76', phase='live', period='first_half',
                                  score_baseline=dict(home=0, away=0, half_home=0, half_away=0)),
                       self.result(0, 0, half_home=0, half_away=0))
        self.assertEqual(('loss', Decimal('-1'), Decimal('1')), (value.outcome, value.unit_net, value.effective_stake))


if __name__ == "__main__":
    unittest.main()
