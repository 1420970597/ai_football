import csv
import re
from pathlib import Path
import unittest

from core.live_model import (clock_seconds, competition_type, fit_share, fit_total,
                             payment, remaining_distribution)
from core.settlement import pnl_for, settle_pick, settle_with_basis, split_line


class LiveModelTests(unittest.TestCase):
    def test_time_is_seconds_and_ambiguous_second_half_is_rejected(self):
        self.assertEqual(clock_seconds('2812', '7'), 2812)
        self.assertEqual(clock_seconds('45:00', '6'), 2700)
        self.assertIsNone(clock_seconds('300', '7'))
        self.assertIsNone(clock_seconds('nan', '6'))
        self.assertIsNone(clock_seconds(True, '6'))

    def test_virtual_and_real_are_separate(self):
        self.assertEqual(competition_type({'league': 'EAFC Battle'}), 'virtual')
        self.assertEqual(competition_type({'league': '英超'}), 'real')
        self.assertEqual(competition_type({}), 'unknown')

    def test_payment_matches_all_asian_settlements(self):
        dist = remaining_distribution(.7, .5, (1, 0))
        for family, outcomes in (('OU', ('over', 'under')), ('AH', ('home', 'away'))):
            for line in (-.75, -.25, 0, .25, 2.25, 2.75):
                for outcome in outcomes:
                    model = payment(dist, family, outcome, line)
                    settled = sum(p * pnl_for(settle_pick(family, outcome, line, (h, a))[0], 1.93)
                                  for h, a, p in dist)
                    self.assertAlmostEqual(model.ev(1.93), settled, places=9)

    def test_score_conditioning_not_future_score(self):
        at_start = payment(remaining_distribution(1, 1, (0, 0)), 'HAD', 'home').win
        leading = payment(remaining_distribution(.1, .1, (1, 0)), 'HAD', 'home').win
        self.assertGreater(leading, at_start)
        self.assertGreater(leading, .90)

    def test_total_inverse_and_home_share(self):
        dist = remaining_distribution(1.2, .8, (1, 0))
        p = payment(dist, 'OU', 'over', 2.25).effective_probability
        total = fit_total(2.25, p, 1)
        self.assertAlmostEqual(total, 2, places=3)
        home = payment(dist, 'HAD', 'home').win
        away = payment(dist, 'HAD', 'away').win
        fitted_h, fitted_a = fit_share(total, (1, 0), home, away)
        self.assertAlmostEqual(fitted_h, 1.2, places=2)
        self.assertAlmostEqual(fitted_a, .8, places=2)

    def test_invalid_distribution_and_market(self):
        with self.assertRaises(ValueError):
            remaining_distribution(float('nan'), 1, (0, 0))
        with self.assertRaises(ValueError):
            payment([(0, 0, 1)], 'OU', 'home', 2.25)
        self.assertIsNone(fit_total('bad', .5, 0))

    def test_zero_goals_mass_and_tail_exposed(self):
        dist = remaining_distribution(0, 0, (2, 1))
        self.assertEqual(dist, ((2, 1, 1.0),))
        result = payment(dist, 'OU', 'over', 3.25)
        self.assertEqual(result.loss, .5)
        self.assertEqual(result.push, .5)



class AuditedSettlementTests(unittest.TestCase):
    def test_signed_composites_preserve_negative_zero_and_explicit_second_sign(self):
        for line, expected in (('-0/0.5', [0,-.5]), ('-0.5/1', [-.5,-1]),
                               ('-0.5/-1', [-.5,-1]), ('-0/+0.5', [0,.5]),
                               ('＋0/0.5', [0,.5]), ('－0/0.5', [0,-.5])):
            self.assertEqual(split_line(line), expected)
        for line in ('-0/NaN', '-0/1', '-0/0.5/1', '--0/.5'):
            self.assertEqual(split_line(line), [])

    def test_160_real_receipts_match_score_benchmark_and_payout(self):
        path = Path(__file__).parents[1]/'reports/auto-betting-loss-audit/data/receipt_comparison.csv'
        with path.open() as source:
            rows = [r for r in csv.DictReader(source) if r['matched_auto']=='1']
        self.assertEqual(len(rows), 160)
        for row in rows:
            with self.subTest(order=row['order_alias']):
                final = tuple(map(int, re.search(r'(\d+)-(\d+)', row['settle_score']).groups()))
                entry = tuple(map(int, row['score_benchmark'].split(':')))
                half = row['native_market'].endswith('_1H')
                status, _ = settle_with_basis(row['native_market'], row['outcome'], row['line'],
                         None if half else final, final if half else None,
                         settlement_basis='remaining_score', entry_score=entry)
                self.assertEqual(status, row['remaining_correct_sign_status'])
                self.assertLessEqual(abs(pnl_for(status, float(row['odds']))*float(row['stake'])-float(row['profit'])), .00500001)

    def test_bad_benchmarks_and_half_score_cannot_be_guessed(self):
        for entry, ft, ht in ((None, (1,0), (1,0)), ((2,0), (1,0), (1,0)),
                               ((1,0), (2,0), None)):
            self.assertEqual(settle_with_basis('AH_1H','home','0',ft,ht,
                             settlement_basis='remaining_score',entry_score=entry)[0], 'void')
        self.assertEqual(settle_with_basis('HAD','home','',(1,0),settlement_basis='full_score')[0], 'won')
        self.assertEqual(settle_with_basis('OU','over','2.5',(2,1),settlement_basis='full_score')[0], 'won')
