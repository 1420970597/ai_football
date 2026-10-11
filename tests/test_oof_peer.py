import unittest
import math
from datetime import datetime, timedelta, timezone

from core.oof_peer import chronological_oof


def rows(matches=40):
    result = []
    for i in range(matches):
        at = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=i * 3)
        day = at.isoformat()
        settled = (at + timedelta(days=1)).isoformat()
        result.append({'match_id': str(i), 'at': day, 'settled_at': settled,
                       'baseline': .4, 'y': i % 2})
    return result


class OofPeerTests(unittest.TestCase):
    def test_predictions_are_temporal_and_match_disjoint_with_safe_warmup_fallback(self):
        observed = []

        def fit(training):
            ids = {r['match_id'] for r in training}
            return ids

        def predict(model, validation):
            valid_ids = {r['match_id'] for r in validation}
            observed.append((model, valid_ids))
            self.assertFalse(model & valid_ids)
            self.assertLess(max(int(i) for i in model), min(int(i) for i in valid_ids))
            return [.7] * len(validation)

        source = rows()
        result = chronological_oof(source, fit, predict, folds=2, min_training_matches=10)
        self.assertEqual(len(result), len(source))
        self.assertEqual(result[:20], [.4] * 20)
        self.assertTrue(all(p == .7 for p in result[20:]))
        self.assertEqual(len(observed), 2)

    def test_invalid_timestamp_or_mismatched_prediction_fails_closed(self):
        source = rows()
        source[0]['at'] = '2025-01-01T00:00:00'
        with self.assertRaises(ValueError):
            chronological_oof(source, lambda _: None, lambda *_: [])

        source = rows()
        with self.assertRaises(ValueError):
            chronological_oof(source, lambda _: None, lambda *_: [], folds=1, min_training_matches=5)

    def test_small_dataset_only_returns_market_fallback(self):
        source = rows(5)
        result = chronological_oof(source, lambda _: self.fail('must not train'),
                                  lambda *_: self.fail('must not predict'), min_training_matches=10)
        self.assertEqual(result, [.4] * 5)


if __name__ == '__main__':
    unittest.main()
