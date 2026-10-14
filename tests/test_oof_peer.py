import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core.oof_peer import chronological_oof, peer_calibrations


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

    def test_oof_fits_graded_labels_and_predicts_entire_validation_context(self):
        base = rows()
        source = []
        for row in base:
            source.extend([row, {**row, 'graded': False, 'y': None, 'observation_weight': 0}])
        source.extend({**row, 'match_id': 'push-only-' + row['match_id'],
                       'graded': False, 'y': None, 'observation_weight': 0} for row in base)
        calls = []

        def fit(training):
            self.assertTrue(all(r['y'] in (0, 1) for r in training))
            self.assertTrue(all(r.get('graded', True) for r in training))
            return {r['match_id'] for r in training}

        def predict(model, validation):
            self.assertFalse(model & {r['match_id'] for r in validation})
            self.assertTrue(any(r.get('graded') is False for r in validation))
            calls.append(len(validation))
            return [.7] * len(validation)

        result = chronological_oof(source, fit, predict, folds=2, min_training_matches=10)
        self.assertEqual(calls, [20, 20])
        self.assertEqual(result[:40], [.4] * 40)
        self.assertEqual(result[40:80], [.7] * 40)
        self.assertEqual(result[80:], [.4] * 40)

    def test_peer_calibration_omits_push_labels(self):
        from core.decision_features import EXTRA_INDEX, PEERS
        from core.learned_model import FEATURES
        source = []
        for row in rows():
            x = [0.] * len(FEATURES)
            x[EXTRA_INDEX[PEERS[0] + '_missing']] = 0
            for peer in PEERS[1:]:
                x[EXTRA_INDEX[peer + '_missing']] = 1
            source.extend([{**row, 'x': x}, {**row, 'x': x, 'graded': False,
                                            'y': None, 'observation_weight': 0}])
        observed = []

        def capture(probs, outcomes):
            self.assertTrue(all(y in (0, 1) for y in outcomes))
            observed.append(len(outcomes))
            return {'method': 'identity', 'samples': len(outcomes)}

        with patch('core.oof_peer.fit_logistic', side_effect=capture):
            model = peer_calibrations(source)
        self.assertEqual(observed[0], 40)
        self.assertEqual(model[PEERS[0]]['samples'], 40)
        self.assertEqual(observed[1:], [0] * 4)


if __name__ == '__main__':
    unittest.main()
