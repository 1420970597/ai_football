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
    def test_active_backup_and_explicit_rollback_require_approved_compatible_model(self):
        import json
        import tempfile
        from pathlib import Path
        from core.learned_model import DATA_VERSION, FEATURES
        from service.model_training import atomic_json, backup_active_model, rollback_active_model

        def artifact(version, approved=True, feature_version=None):
            return {'version': version, 'features': list(FEATURES), 'weights': [0.0] * len(FEATURES),
                    'data_version': DATA_VERSION, 'feature_version': feature_version or
                    __import__('core.decision_features', fromlist=['FEATURE_VERSION']).FEATURE_VERSION,
                    'updated_at': '2025-01-01T00:00:00+00:00',
                    'promotion': {'approved': approved}}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active = root / 'active.json'
            atomic_json(active, artifact('old-approved'))
            backup = backup_active_model(root)
            self.assertEqual(backup.name, 'active-backup-old-approved.json')
            atomic_json(active, artifact('new-active'))
            self.assertTrue(rollback_active_model(root, 'old-approved')['restored'])
            self.assertEqual(json.loads(active.read_text())['version'], 'old-approved')

            atomic_json(root / 'active-backup-unapproved.json', artifact('unapproved', approved=False))
            result = rollback_active_model(root, 'unapproved')
            self.assertFalse(result['restored'])
            self.assertEqual(json.loads(active.read_text())['version'], 'old-approved')

            atomic_json(root / 'active-backup-incompatible.json',
                        artifact('incompatible', feature_version='unknown-feature-schema'))
            self.assertFalse(rollback_active_model(root, 'incompatible')['restored'])
            self.assertEqual(json.loads(active.read_text())['version'], 'old-approved')

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

    def test_had_top1_counts_each_decision_then_weights_matches_equally(self):
        source = []
        # Match A has two distinct decision timestamps: one correct, one wrong.
        # Match B has one correct decision. Match-level weighting therefore gives
        # (0.5 + 1.0) / 2, independent of the number of decisions in match A.
        groups = [
            ('A', '12:00', (.7, .2, .1), (1, 0, 0)),
            ('A', '12:01', (.7, .2, .1), (0, 0, 1)),
            ('B', '12:00', (.2, .2, .6), (0, 0, 1)),
        ]
        for match, at, probabilities, labels in groups:
            for outcome, probability, label in zip(('home', 'draw', 'away'), probabilities, labels):
                source.append({'match_id': match, 'at': at, 'settled_at': '13:00',
                               'market': 'HAD', 'line': '', 'algorithm': 'test',
                               'outcome': outcome, 'y': label, 'p': probability})
        result = evaluate(source, [row['p'] for row in source])
        self.assertEqual(result['had_top1_matches'], 2)
        self.assertEqual(result['complete_had_groups'], 3)
        self.assertAlmostEqual(result['had_top1_accuracy'], .75)

    def test_incomplete_had_and_invalid_top1_groups_are_excluded(self):
        source = [{'match_id': 'A', 'at': 't', 'market': 'HAD', 'line': '', 'algorithm': 'x',
                   'outcome': 'home', 'y': 1}]
        source.extend({'match_id': 'B', 'at': 't', 'market': 'HAD', 'line': '', 'algorithm': 'x',
                       'outcome': outcome, 'y': label}
                      for outcome, label in (('home', 1), ('draw', 1), ('away', 0)))
        result = evaluate(source, [.8, .1, .1, .2])
        self.assertIsNone(result['had_top1_accuracy'])
        self.assertEqual(result['had_top1_matches'], 0)
        self.assertEqual(result['complete_had_groups'], 0)

    def test_paired_bootstrap_and_promotion_reject_small_or_uncertain_data(self):
        source = rows(30)
        evidence = paired_evidence(source, [.6] * len(source), [.5] * len(source))
        self.assertEqual(evidence['matches'], 30)
        decision = promotion({'log_loss': .5, 'by_market': {}}, {'log_loss': .4, 'by_market': {}}, evidence, [])
        self.assertFalse(decision['approved'])
        self.assertTrue(decision['reasons'])


if __name__ == '__main__':
    unittest.main()
