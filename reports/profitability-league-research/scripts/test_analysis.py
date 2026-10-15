#!/usr/bin/env python3
"""Checks for financial denominator mistakes and incomplete public quotes."""
import math
import unittest
from decimal import Decimal

from analyze import fair_probabilities, historical_summary, league_summary


class AnalysisTests(unittest.TestCase):
    def test_pending_excluded_and_market_totals_reconcile(self):
        rows = [dict(match_id='a', market='AH', stake='2', sim_status='half_won', sim_pnl_per_unit='.4'),
                dict(match_id='a', market='OU', stake='3', sim_status='lost', sim_pnl_per_unit='-1'),
                dict(match_id='b', market='OU', stake='100', sim_status='pending', sim_pnl_per_unit='0')]
        result = historical_summary(rows)
        self.assertEqual((result['all_rows'], result['orders'], result['matches'], result['pending_excluded']), (3, 2, 1, 1))
        self.assertEqual(Decimal(result['stake']), Decimal(5))
        self.assertEqual(Decimal(result['net_recomputed']), Decimal('-2.2'))
        self.assertEqual(sum(Decimal(r['stake']) for r in result['by_market']), Decimal(result['stake']))

    def test_empty_and_zero_stake_history_rejected(self):
        for rows in ([], [dict(match_id='a', market='OU', stake='0', sim_status='push', sim_pnl_per_unit='0')]):
            with self.assertRaises(ValueError):
                historical_summary(rows)

    def test_same_quote_devig_identity(self):
        odds = [2.2, 3.2, 3.5]
        ps, margin = fair_probabilities(odds)
        self.assertAlmostEqual(sum(ps), 1)
        self.assertGreater(margin, 0)
        for p, o in zip(ps, odds):
            self.assertAlmostEqual(p * o - 1, 1 / (1 + margin) - 1)

    def test_nonfinite_and_invalid_quotes_rejected(self):
        for odds in ([2, 3], [1, 2, 3], [0, 2, 3], [math.nan, 2, 3], [math.inf, 2, 3]):
            with self.assertRaises(ValueError):
                fair_probabilities(odds)

    def test_public_missing_odds_keep_match_denominator(self):
        rows = [dict(FTHG='1', FTAG='1', FTR='D', B365H='', B365D='', B365A=''),
                dict(FTHG='2', FTAG='0', FTR='H', B365H='2', B365D='3', B365A='4')]
        result = league_summary('E0', rows)
        self.assertEqual((result['matches'], result['odds_complete']), (2, 1))
        self.assertEqual(result['draw_rate'], .5)
        self.assertAlmostEqual(result['market_logloss'], round(-math.log(6 / 13), 6))

    def test_empty_and_inconsistent_public_result_rejected(self):
        for rows in ([], [dict(FTHG='1', FTAG='0', FTR='D')]):
            with self.assertRaises(ValueError):
                league_summary('E0', rows)


if __name__ == '__main__':
    unittest.main()
