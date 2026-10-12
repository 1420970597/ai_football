#!/usr/bin/env python3
"""Offline reproductions of actual decision contracts, without venue calls."""
import json
from pathlib import Path
import sys
import unittest

REPO = Path(__file__).resolve().parents[3]
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit import grade, inherited_line, score, write_csv, split_line
from core.economics import shrink_probabilities
from core.settlement import pnl_for
from service.betting import plan_bet, BettingBlocked
from service.runtime_settings import RuntimeConfig

PROBABILITIES = (0.1, 0.3, 0.5, 0.7, 0.9)
PRIOR = 0.5
SHRINK_N = 1
SHRINK_K = 2.0
STAKE = 5.0
KELLY_EXAMPLES = (0.001, 0.05, 0.25)
LINES = ('-0/0.5', '-0.5/1', '0/0.5', '0.5/1', '-0.5/-1')


class AuditChecks(unittest.TestCase):
    def test_remaining_score_and_sign_explain_receipt(self):
        self.assertEqual(grade('AH','home','-0/0.5',(1,0)), 'won')
        self.assertEqual(grade('AH','home','-0/0.5',(1,0),(1,0)), 'half_won')
        self.assertEqual(grade('AH','home','-0/0.5',(1,0),(1,0),inherit=True), 'half_lost')
        self.assertAlmostEqual(pnl_for('half_lost',1.49)*STAKE,-2.5)

    def test_explicit_signed_halves_and_invalid_lines(self):
        self.assertEqual(split_line(inherited_line('-0.5/-1')),[-0.5,-1.0])
        self.assertEqual(split_line(inherited_line('0/0.5')),[0,0.5])
        for value in ('','bad','nan','0/1','0/0.5/1'):
            with self.assertRaises(ValueError): inherited_line(value)

    def test_scores_missing_or_decreasing_are_not_invented(self):
        self.assertIsNone(score('未知'))
        self.assertEqual(score('上半场比分 2-0'),(2,0))
        self.assertEqual(grade('AH','home','0',None),'void')
        self.assertEqual(grade('AH','home','0',(0,0),(1,0)), 'void')
        self.assertEqual(grade('AH_1H','home','0',(2,0),(2,0)), 'push')
        self.assertEqual(grade('OU','over','2.5',(2,1),(1,0)), 'won')

    def test_singleton_probability_current_contract_is_reproduced(self):
        # A passing diagnostic proves the current defect exists, not that it is correct.
        value=shrink_probabilities((0.6,),(0.5,),SHRINK_N,k=SHRINK_K)[0][0]
        self.assertEqual(value,1.0)
        corrected=shrink_probabilities((0.6,0.4),(0.5,0.5),SHRINK_N,k=SHRINK_K)[0][0]
        self.assertAlmostEqual(corrected,0.5333333333333333)

    def test_fixed_stake_ignores_kelly_and_bad_hit_rate(self):
        cfg=RuntimeConfig(betting_enabled=True,betting_fixed_stake=STAKE,betting_min_hit_count=5)
        pick={'algorithm':'economic_ensemble','match_id':'offline', 'market':'AH','line':'0',
              'outcome':'home','hit_count':5,'miss_count':995,'confidence':0.01,'kelly':0.001}
        # The original CSV documents fixed-stake sizing at the audited commit.
        # Current production now blocks uncertified/losing historical evidence.
        with self.assertRaises(BettingBlocked): plan_bet(pick,cfg)
        with self.assertRaises(BettingBlocked): plan_bet({**pick,'hit_count':4},cfg)
        with self.assertRaises(BettingBlocked): plan_bet(pick,cfg,market_open=False)


def main():
    result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(AuditChecks))
    if not result.wasSuccessful(): return 1
    weight=SHRINK_N/(SHRINK_N+SHRINK_K)
    rows=[{'input_model':p,'input_prior':PRIOR,'weight':weight,
           'production':shrink_probabilities((p,),(PRIOR,),SHRINK_N,k=SHRINK_K)[0][0],
           'scalar_expected':weight*p+(1-weight)*PRIOR,
           'binary_vector':shrink_probabilities((p,1-p),(PRIOR,1-PRIOR),SHRINK_N,k=SHRINK_K)[0][0]}
          for p in PROBABILITIES]
    write_csv(ROOT/'data'/'shrink_reproduction.csv',rows)
    write_csv(ROOT/'data'/'negative_line_reproduction.csv',[
        {'raw_line':line,'production_parts':json.dumps(split_line(line)),
         'canonical_parts':json.dumps(split_line(inherited_line(line)))} for line in LINES])
    cfg=RuntimeConfig(betting_enabled=True,betting_fixed_stake=STAKE,betting_min_hit_count=5)
    pick={'algorithm':'economic_ensemble','match_id':'offline','market':'AH','line':'0',
          'outcome':'home','hit_count':5,'miss_count':995,'confidence':0.01}
    write_csv(ROOT/'data'/'fixed_stake_reproduction.csv',[
        {'kelly':kelly,'hit_count':5,'miss_count':995,'confidence':0.01,
         'actual_plan_stake':cfg.betting_fixed_stake} for kelly in KELLY_EXAMPLES])
    return 0


if __name__ == '__main__':
    sys.exit(main())
