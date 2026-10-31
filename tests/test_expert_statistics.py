from decimal import Decimal
import unittest

from app.expert_statistics import metrics


class ExpertStatisticsTests(unittest.TestCase):
    def test_six_outcomes_use_effective_stake_not_pending_as_accuracy_denominator(self):
        value = metrics({'win':1,'half_win':1,'loss':1,'half_loss':1,'push':1,'void':1,
                         'pending':79,'total_signals':85,'effective_stake':Decimal('3'),
                         'unit_net':Decimal('-.15')})
        self.assertEqual(6,value['settled_signals'])
        self.assertEqual(.5,value['accuracy'])
        self.assertEqual(-.025,value['average_unit_net'])
        self.assertEqual(-.05,value['unit_roi'])
        self.assertEqual('limited_sample',value['sample_state'])

    def test_empty_and_neutral_samples_never_claim_perfect_accuracy_or_roi(self):
        empty=metrics({})
        self.assertEqual('no_settled_evidence',empty['sample_state'])
        for key in ('accuracy','average_unit_net','unit_roi'):
            self.assertIsNone(empty[key])
        neutral=metrics({'push':30,'void':2,'effective_stake':0,'unit_net':0})
        self.assertIsNone(neutral['accuracy'])
        self.assertIsNone(neutral['unit_roi'])
        self.assertEqual(0,neutral['average_unit_net'])
        self.assertEqual('observed_sample',neutral['sample_state'])
