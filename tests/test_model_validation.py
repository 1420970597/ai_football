import unittest
from datetime import datetime, timedelta, timezone

from core.model_validation import (VALIDATION_CONTRACT_VERSION, coherent_probabilities,
                                   evaluate, execution_evidence, is_graded_row,
                                   ordered_matches, paired_evidence, promotion, rolling_folds)


def approved_artifact(version='synthetic-approved', **changes):
    """Synthetic gate evidence for loader tests; never a deployable model."""
    from core.decision_features import FEATURE_VERSION
    from core.learned_model import DATA_VERSION, FEATURES
    score = {'matches': 120, 'decisions': 240, 'brier': .04,
             'log_loss': .223, 'by_market': {}}
    baseline = {**score, 'brier': .2025, 'log_loss': .598}
    paired = {'matches': 120, 'delta_brier': -.1625, 'ci95': [-.1625, -.1625]}
    evidence = {**paired, 'execution_cohort': {'available': True,
        'algorithm': 'economic_ensemble', 'model': score, 'baseline': baseline,
        'evidence': dict(paired)}}
    folds = [{'delta_brier': -.1625} for _ in range(3)]
    return {'version': version, 'features': list(FEATURES),
            'weights': [0.] * len(FEATURES), 'data_version': DATA_VERSION,
            'feature_version': FEATURE_VERSION,
            'validation_contract_version': VALIDATION_CONTRACT_VERSION,
            'updated_at': '2025-01-01T00:00:00+00:00',
            'validation': score, 'baseline': baseline, 'rolling_validation': folds,
            'promotion': promotion(score, baseline, evidence, folds), **changes}


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
    def execution_case(self, execution_probability=.8, count=120, include_execution=True):
        source, candidate, baseline = [], [], []
        algorithms = ['a', 'b', 'c', 'd', 'e'] + (['economic_ensemble'] if include_execution else [])
        for match in range(count):
            for algorithm in algorithms:
                for outcome, target in [('over', 1), ('under', 0)]:
                    source.append({'match_id': str(match), 'at': 't', 'algorithm': algorithm,
                                   'market': 'OU', 'line': '2.5', 'outcome': outcome, 'y': target})
                    probability = execution_probability if algorithm == 'economic_ensemble' else .8
                    candidate.append(probability if target else 1 - probability)
                    baseline.append(.55 if target else .45)
        test, market = evaluate(source, candidate), evaluate(source, baseline)
        evidence = paired_evidence(source, candidate, baseline)
        evidence['execution_cohort'] = execution_evidence(source, candidate, baseline)
        folds = [{'delta_brier': test['brier'] - market['brier']}] * 3
        return test, market, evidence, folds

    def test_actual_execution_subgroup_can_certify_normal_promotion(self):
        test, market, evidence, folds = self.execution_case()
        self.assertEqual(test['by_algorithm']['economic_ensemble']['matches'], 120)
        self.assertLess(test['by_algorithm']['economic_ensemble']['brier'],
                        market['by_algorithm']['economic_ensemble']['brier'])
        decision = promotion(test, market, evidence, folds)
        self.assertTrue(decision['approved'], decision['reasons'])
        self.assertTrue(decision['execution_approved'])
        self.assertTrue(decision['execution_required'])

    def test_aggregate_improvement_cannot_hide_execution_subgroup_regression(self):
        test, market, evidence, folds = self.execution_case(.35)
        self.assertLess(evidence['ci95'][1], 0)
        self.assertLess(test['log_loss'], market['log_loss'])
        self.assertLess(test['brier'], market['brier'])
        execution = evidence['execution_cohort']
        self.assertGreater(execution['model']['brier'], execution['baseline']['brier'])
        decision = promotion(test, market, evidence, folds)
        self.assertFalse(decision['approved'])
        self.assertFalse(decision['execution_approved'])
        self.assertTrue(any('实际执行算法' in reason for reason in decision['reasons']))

    def test_missing_or_small_execution_cohort_cannot_promote(self):
        for include_execution, count in [(False, 120), (True, 30)]:
            with self.subTest(include_execution=include_execution, count=count):
                test, market, evidence, folds = self.execution_case(
                    count=count, include_execution=include_execution)
                decision = promotion(test, market, evidence, folds)
                self.assertFalse(decision['approved'])
                self.assertFalse(decision['execution_approved'])
        test, market, evidence, folds = self.execution_case()
        del evidence['execution_cohort']
        decision = promotion(test, market, evidence, folds)
        self.assertFalse(decision['approved'])
        self.assertIn('实际执行算法缺少独立测试证据，不能晋级', decision['reasons'])

    def test_execution_market_regression_is_rejected_despite_execution_aggregate_gain(self):
        source, candidate, baseline = [], [], []
        for match in range(120):
            for market, line, probability, outcomes in [
                    ('OU', '2.5', .35, ('over', 'under')),
                    ('AH', '0.5', .95, ('home', 'away')),
                    ('AH', '1.5', .95, ('home', 'away')),
                    ('AH', '2.5', .95, ('home', 'away'))]:
                for outcome, target in zip(outcomes, (1, 0)):
                    source.append({'match_id': str(match), 'at': 't', 'algorithm': 'economic_ensemble',
                                   'market': market, 'line': line, 'outcome': outcome, 'y': target})
                    candidate.append(probability if target else 1 - probability)
                    baseline.append(.55 if target else .45)
        test, market = evaluate(source, candidate), evaluate(source, baseline)
        evidence = paired_evidence(source, candidate, baseline)
        evidence['execution_cohort'] = execution_evidence(source, candidate, baseline)
        self.assertLess(evidence['ci95'][1], 0)
        decision = promotion(test, market, evidence, [{'delta_brier': -.01}] * 3)
        self.assertFalse(decision['approved'])
        self.assertIn('实际执行算法：OU测试表现明显退化', decision['reasons'])

    def test_active_backup_and_explicit_rollback_require_approved_compatible_model(self):
        import json
        import tempfile
        from pathlib import Path
        from service.model_training import atomic_json, backup_active_model, rollback_active_model

        def artifact(version, approved=True, feature_version=None):
            result = approved_artifact(version)
            result['promotion']['approved'] = approved
            if feature_version:
                result['feature_version'] = feature_version
            return result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active = root / 'active.json'
            atomic_json(active, artifact('old-approved'))
            backup = backup_active_model(root)
            self.assertEqual(backup.name, 'active-backup-old-approved.json')
            atomic_json(active, artifact('new-active'))
            self.assertTrue(rollback_active_model(root, 'old-approved')['restored'])
            self.assertEqual(json.loads(active.read_text())['version'], 'old-approved')

            legacy = artifact('legacy-approved')
            legacy.pop('validation_contract_version')
            legacy_path = root / 'active-backup-legacy-approved.json'
            atomic_json(legacy_path, legacy)
            self.assertFalse(rollback_active_model(root, 'legacy-approved')['restored'])
            self.assertTrue(legacy_path.exists())
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

    def test_push_curve_context_is_projected_before_loss_mask(self):
        source = []
        for line, labels in [('2.5', (1, 0)), ('3', (None, None))]:
            for outcome, target in zip(('over', 'under'), labels):
                source.append({**rows(1)[0], 'market': 'OU', 'line': line,
                               'outcome': outcome, 'y': target,
                               'graded': target is not None,
                               'observation_weight': 1 if target is not None else 0})
        ps = [.4, .6, .7, .3]
        result = evaluate(source, ps)
        self.assertEqual(result['decisions'], 2)
        self.assertEqual(result['prediction_context_decisions'], 4)
        self.assertEqual(result['excluded_from_binary_loss'], 2)
        self.assertAlmostEqual(result['brier'], .45 ** 2)
        baseline = [.5] * 4
        evidence = paired_evidence(source, ps, baseline)
        self.assertAlmostEqual(evidence['delta_brier'], .45 ** 2 - .25)
        without_context = evaluate(source[:2], ps[:2])
        self.assertAlmostEqual(without_context['brier'], .6 ** 2)

    def test_all_push_context_has_no_binary_score_or_promotion(self):
        source = [{**rows(1)[0], 'y': None, 'graded': False, 'observation_weight': 0}]
        result = evaluate(source, [.8])
        self.assertEqual(result['matches'], 0)
        self.assertEqual(result['decisions'], 0)
        self.assertIsNone(result['accuracy'])
        self.assertIsNone(result['brier'])
        evidence = paired_evidence(source, [.8], [.5])
        self.assertEqual(evidence['ci95'], [None, None])
        self.assertFalse(promotion(result, result, evidence, [])['approved'])
        self.assertEqual(ordered_matches(source), [])
        execution = execution_evidence([{**source[0], 'algorithm': 'economic_ensemble'}], [.8], [.5])
        self.assertFalse(execution['available'])

    def test_invalid_binary_masks_cannot_fake_labels_or_weights(self):
        for update in ({'graded': 1}, {'graded': False, 'y': 0},
                       {'graded': False, 'y': None, 'observation_weight': 1},
                       {'graded': True, 'y': None}, {'graded': True, 'observation_weight': 0}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                is_graded_row({**rows(1)[0], **update})

    def test_nonfinite_evidence_cannot_approve_an_artifact(self):
        test, market, evidence, folds = self.execution_case()
        evidence['ci95'][1] = float('nan')
        self.assertFalse(promotion(test, market, evidence, folds)['approved'])
        test['log_loss'] = float('nan')
        self.assertFalse(promotion(test, market, evidence, folds)['approved'])

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
