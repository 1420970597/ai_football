"""Real model behavior: grouped time validation, calibration and feature boundaries."""
from datetime import datetime, timedelta, timezone
import unittest

from core.learned_model import FEATURES, features, metrics, predict, train


def sample_row(index=0):
    at = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
    source = {'market': 'HAD', 'outcome': 'home', 'line': '', 'algorithm': 'poisson_time_decay',
              'entry_score': [0, 0], 'entry_clock_s': 1200, 'p_market': .6, 'p_fused': .7}
    return {'match_id': str(index), 'at': at.isoformat(), 'settled_at': (at + timedelta(hours=2)).isoformat(),
            'x': features(source), 'baseline': .6, 'y': int(index % 3 != 0)}


class LearnedModelTests(unittest.TestCase):
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
        for patch in ({'entry_clock_s': 5400}, {'p_fused': float('nan')}, {'line': 'bad'}, {'entry_score': None}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                features({**row, **patch})
        with self.assertRaises(ValueError):
            predict([0], x)
