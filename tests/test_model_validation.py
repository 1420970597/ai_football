import unittest
from datetime import datetime, timedelta, timezone

from core.model_validation import (coherent_probabilities, evaluate, paired_evidence,
                                   promotion, rolling_folds)


def rows(count=120):
    result = []
    for i in range(count):
        at = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
        result.append({'match_id': str(i), 'at': at.isoformat(),
                       'settled_at': (at + timedelta(hours=2)).isoformat(),
                       'market': 'HAD', 'line': '', 'algorithm': 'test',
                       'outcome': 'home', 'y': i % 2, 'observation_weight': 1})
    return result


class ModelValidationTests(unittest.TestCase):
    def test_rolling_folds_are_time_and_label_separated(self):
        folds = rolling_folds(rows())
        self.assertGreaterEqual(len(folds), 2)
        for training, validation in folds:
            self.assertLess(max(r['settled_at'] for r in training), min(r['at'] for r in validation))

    def test_complete_had_group_is_normalized_and_top1_is_separate(self):
        source = []
        for outcome, probability, y in (('home', .6, 0), ('draw', .2, 0), ('away', .2, 1)):
            source.append({**rows(1)[0], 'outcome': outcome, 'y': y,
                           'market': 'HAD', 'algorithm': 'test', 'p': probability})
        p = coherent_probabilities(source, [r['p'] for r in source])
        self.assertAlmostEqual(sum(p), 1)
        result = evaluate(source, p)
        self.assertFalse(result['had_top1_accuracy'])
        self.assertIn('binary_accuracy', result)

    def test_paired_bootstrap_and_promotion_reject_small_or_uncertain_data(self):
        source = rows(30)
        evidence = paired_evidence(source, [.6] * len(source), [.5] * len(source))
        self.assertEqual(evidence['matches'], 30)
        decision = promotion({'log_loss': .5, 'by_market': {}}, {'log_loss': .4, 'by_market': {}}, evidence, [])
        self.assertFalse(decision['approved'])
        self.assertTrue(decision['reasons'])


if __name__ == '__main__':
    unittest.main()
