import unittest

from core.decision_features import EXTRA_FEATURES, upgrade_vector, visible_history
from core.learned_model import features


def source(history=None):
    return {'market': 'OU', 'outcome': 'over', 'line': '2.5', 'entry_score': [1, 0],
            'entry_clock_s': 1200, 'p_market': .55, 'p_fused': .57,
            'training_history': ([[1000, 2.0], [1010, 2.1], [1100, 1.9], [1190, 1.8]]
                                 if history is None else history),
            'input_cutoff_ms': 1200}


class DecisionFeatureTests(unittest.TestCase):
    def test_window_features_use_elapsed_time_and_preserve_missing(self):
        short = features(source([[1000, 2.0], [1010, 2.1]]))
        long = features(source([[1000, 2.0], [1190, 2.1]]))
        self.assertNotEqual(short[-len(EXTRA_FEATURES):], long[-len(EXTRA_FEATURES):])
        missing = features(source([]))
        self.assertTrue(all(missing[30 + i] == 1 for i in (4, 9, 14)))

    def test_future_history_is_removed_by_both_times(self):
        row = source([[1000, 2], [1100, 2.1, 1300]])
        self.assertEqual([point[0] for point in visible_history(row)], [1000])

    def test_upgrade_legacy_and_reject_nonfinite(self):
        legacy = [0.0] * 30
        upgraded = upgrade_vector(legacy)
        self.assertEqual(len(upgraded), 60)
        with self.assertRaises(ValueError):
            upgrade_vector([float('nan')] * 30)

    def test_verified_red_cards_only_accept_explicit_normalized_input(self):
        from core.decision_features import verified_context
        self.assertEqual(verified_context({'red_cards': [1, 0]})['red_card_difference'], 1)
        self.assertNotIn('red_card_difference', verified_context({'events': [{'code': 7}]}))


if __name__ == '__main__':
    unittest.main()
