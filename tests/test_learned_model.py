"""Real model behavior: grouped time validation, calibration and feature boundaries."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from core.learned_model import (FEATURES, STACK_INDICES, approved_model, compatible_model,
                                features, fit_weights, metrics, model_compatibility_reason,
                                predict, train)
from core.model_validation import VALIDATION_CONTRACT_VERSION
from tests.test_model_validation import approved_artifact


def sample_row(index=0):
    at = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
    source = {'market': 'HAD', 'outcome': 'home', 'line': '', 'algorithm': 'poisson_time_decay',
              'entry_score': [0, 0], 'entry_clock_s': 1200, 'p_market': .6, 'p_fused': .7}
    return {'match_id': str(index), 'at': at.isoformat(), 'settled_at': (at + timedelta(hours=2)).isoformat(),
            'x': features(source), 'baseline': .6, 'y': int(index % 3 != 0)}


class LearnedModelTests(unittest.TestCase):
    def test_candidate_compatibility_and_approval_are_separate_current_contracts(self):
        model = approved_artifact()
        self.assertTrue(compatible_model(model))
        self.assertTrue(approved_model(model))
        candidate = {**model, 'promotion': {'approved': False}}
        self.assertTrue(compatible_model(candidate))
        self.assertFalse(approved_model(candidate))
        for field in ('data_version', 'validation_contract_version'):
            with self.subTest(field=field):
                legacy = {**model, field: 'legacy'}
                self.assertFalse(compatible_model(legacy))
                self.assertFalse(approved_model(legacy))
                self.assertTrue(model_compatibility_reason(legacy))
        for field, value in [('validation_contract_version', 'legacy'),
                             ('execution_required', False), ('execution_approved', False),
                             ('execution_algorithm', 'auxiliary')]:
            with self.subTest(promotion_field=field):
                invalid = {**model, 'promotion': {**model['promotion'], field: value}}
                self.assertTrue(compatible_model(invalid))
                self.assertFalse(approved_model(invalid))

    def test_approval_flags_cannot_override_execution_evidence_regression(self):
        model = approved_artifact()
        evidence = model['promotion']['evidence']['execution_cohort']
        evidence['model'] = {**evidence['model'], 'log_loss': 1.5}
        self.assertTrue(model['promotion']['approved'])
        self.assertFalse(approved_model(model))
        self.assertIn('实际执行算法', model_compatibility_reason(model, require_approval=True))

    def test_ungraded_context_does_not_fit_or_count_as_training_match(self):
        rows = [sample_row(i) for i in range(120)]
        pushes = [{**sample_row(i), 'graded': False, 'y': None, 'observation_weight': 0}
                  for i in range(130)]
        expected = fit_weights(rows, epochs=2)
        actual = fit_weights(rows + pushes, epochs=2)
        self.assertEqual(expected, actual)
        self.assertTrue(all(value == 0 for index, value in enumerate(actual)
                            if index not in STACK_INDICES))
        model = train(rows + pushes, epochs=2)
        self.assertEqual(model['eligible_matches'], 120)
        self.assertEqual(model['validation']['matches'], 24)
        self.assertEqual(model['validation']['prediction_context_decisions'], 48)
        self.assertEqual(model['validation_contract_version'], VALIDATION_CONTRACT_VERSION)
        with self.assertRaises(ValueError):
            fit_weights(pushes)
        empty_metrics = metrics(pushes, None)
        self.assertIsNone(empty_metrics['brier'])
        self.assertEqual(empty_metrics['matches'], 0)

    def test_half_payout_scores_use_nonpush_stake_weight(self):
        rows = [{**sample_row(0),'y':1,'baseline':.8,'observation_weight':1},
                {**sample_row(0),'y':0,'baseline':.8,'observation_weight':.5}]
        result = metrics(rows,None)
        self.assertAlmostEqual(result['accuracy'],2/3)
        self.assertAlmostEqual(result['brier'],(.04+.5*.64)/1.5)
        with self.assertRaises(ValueError):
            metrics([{**rows[0],'observation_weight':0}],None)

    def test_metrics_match_live_complete_group_normalization(self):
        rows = []
        for outcome, probability, target in (('home', .6, 1), ('draw', .3, 0), ('away', .3, 0)):
            source = {'market': 'HAD', 'outcome': outcome, 'line': '',
                      'algorithm': 'poisson_time_decay', 'entry_score': [0, 0],
                      'entry_clock_s': 1200, 'p_market': probability, 'p_fused': probability}
            rows.append({**sample_row(0), 'market': 'HAD', 'outcome': outcome,
                         'algorithm': 'poisson_time_decay', 'line': '',
                         'x': features(source), 'baseline': probability, 'y': target})
        result = metrics(rows, None)
        # Live inference normalizes .6/.3/.3 to .5/.25/.25 before scoring.
        self.assertAlmostEqual(result['brier'], (.25 + .0625 + .0625) / 3)

    def test_train_and_group_split_retains_multiple_decisions(self):
        rows = [sample_row(i) for i in range(120)]
        for i in range(120):
            second = {**sample_row(i), 'at': (datetime.fromisoformat(rows[i]['at']) + timedelta(minutes=20)).isoformat()}
            rows.append(second)
        model = train(rows)
        self.assertEqual(model['parameter_count'], len(FEATURES))
        self.assertEqual(model['training']['matches'], 96)
        self.assertEqual(model['training']['decisions'], 192)
        self.assertEqual(model['validation']['matches'], 24)
        self.assertEqual(model['validation']['decisions'], 48)
        self.assertLess(model['training_cutoff'], model['validation_from'])
        self.assertGreater(predict(model['weights'], rows[0]['x']), 0)
        self.assertLess(predict(model['weights'], rows[0]['x']), 1)

    def test_match_normalization_is_invariant_to_repeated_decisions(self):
        rows = [sample_row(i) for i in range(100)]
        repeated = rows + [dict(rows[0]) for _ in range(20)]
        a, b = train(rows), train(repeated)
        for wa, wb in zip(a['weights'], b['weights']):
            self.assertAlmostEqual(wa, wb, places=10)
        weights = [0.0] * len(FEATURES)
        self.assertAlmostEqual(metrics(rows, weights)['brier'], metrics(repeated, weights)['brier'])

    def test_no_future_settlement_labels_in_training(self):
        rows = [sample_row(i) for i in range(100)]
        for row in rows[:30]:
            row['settled_at'] = rows[-1]['settled_at']
        with self.assertRaisesRegex(ValueError, '时间隔离'):
            train(rows)
        with self.assertRaisesRegex(ValueError, '100'):
            train(rows[:10])

    def test_explicit_final_partition_keeps_epochs_and_membership(self):
        rows = [sample_row(i) for i in range(120)]
        from core.model_validation import rolling_folds
        partition = rolling_folds(rows)[-1]
        model = train(rows, partition=partition, epochs=2)
        self.assertEqual(model['training_epochs'], 2)
        self.assertEqual(set(model['training_match_ids']), {r['match_id'] for r in partition[0]})
        self.assertEqual(set(model['validation_match_ids']), {r['match_id'] for r in partition[1]})
        self.assertEqual(model['validation']['matches'], len(partition[1]))
        self.assertLess(model['training_cutoff'], model['validation_from'])

    def test_explicit_partition_rejects_overlap_and_unavailable_labels(self):
        rows = [sample_row(i) for i in range(120)]
        with self.assertRaisesRegex(ValueError, '重复比赛'):
            train(rows, partition=(rows[:100], rows[99:]))
        training = [dict(r) for r in rows[:100]]
        training[0]['settled_at'] = rows[100]['at']
        with self.assertRaisesRegex(ValueError, '尚不可用'):
            train(rows, partition=(training, rows[100:]))

    def test_final_partition_fit_receives_explicit_epoch_budget(self):
        rows = [sample_row(i) for i in range(120)]
        with patch('core.learned_model.fit_weights', return_value=[0.] * len(FEATURES)) as fit:
            train(rows, partition=(rows[:100], rows[100:]), epochs=7)
        self.assertEqual(fit.call_args.kwargs['epochs'], 7)

    def test_multi_market_features_and_missingness(self):
        row = {'market': 'OU_1H', 'outcome': 'over', 'line': '2/2.5', 'entry_score': [1, 0],
               'entry_clock_s': 1500, 'p_market': .5, 'p_fused': .6,
               'training_trend_pct': 5, 'training_history': [[1000, 2], [2000, 2.1]]}
        x = features(row)
        self.assertEqual(len(x), len(FEATURES))
        self.assertEqual(x[FEATURES.index('half')], 1)
        self.assertEqual(x[FEATURES.index('ou')], 1)
        self.assertEqual(x[FEATURES.index('trend_missing')], 0)
        self.assertGreater(x[FEATURES.index('trend_volatility')], 0)
        row.pop('training_trend_pct')
        self.assertEqual(features(row)[FEATURES.index('trend_missing')], 1)
        for override in ({'entry_clock_s': 5400}, {'p_fused': float('nan')}, {'line': 'bad'}, {'entry_score': None}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                features({**row, **override})
        with self.assertRaises(ValueError):
            predict([0], x)
