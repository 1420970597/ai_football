"""Synthetic safety tests for isolated P2 challenger primitives."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from core.model_challengers import (dynamic_poisson_distribution, fit_market_stump,
                                    predict_market_stump, validate_training_rows,
                                    visible_match_events)


def training_rows() -> list[dict]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(24):
        high = i % 2 == 1
        decision = base + timedelta(minutes=i)
        rows.append({'match_id': f'match-{i}', 'at': decision.isoformat(),
                     'label_available_at': (decision + timedelta(hours=2)).isoformat(),
                     'cutoff_ms': 1000, 'received_at_ms': 900,
                     'model_available_at_ms': 950, 'baseline': .5,
                     'y': int(high), 'features': {'signal': float(high)}})
    return rows


class ModelChallengersTests(unittest.TestCase):
    def test_market_offset_stump_learns_synthetic_signal_and_keeps_baseline_offset(self):
        rows = training_rows()
        model = fit_market_stump(rows, feature='signal')
        self.assertEqual(model['depth'], 1)
        self.assertFalse(model['production_eligible'])
        low = predict_market_stump(model, .5, 0)
        high = predict_market_stump(model, .5, 1)
        self.assertLess(low, .5)
        self.assertGreater(high, .5)
        shifted = predict_market_stump(model, .4, 0)
        self.assertAlmostEqual(shifted / (1-shifted), (.4 / .6) * (low / (1-low)))

    def test_fit_rejects_missing_or_future_information_and_nonisolated_label(self):
        cases = []
        missing = training_rows()
        del missing[0]['model_available_at_ms']
        cases.append(missing)
        late = training_rows()
        late[0]['received_at_ms'] = 1001
        cases.append(late)
        early_label = training_rows()
        early_label[0]['label_available_at'] = early_label[0]['at']
        cases.append(early_label)
        for rows in cases:
            with self.subTest(row=rows[0]):
                with self.assertRaises(ValueError):
                    validate_training_rows(rows)
                with self.assertRaises(ValueError):
                    fit_market_stump(rows, feature='signal')

    def test_event_filter_excludes_late_arrivals_and_dynamic_poisson_uses_visible_red_card(self):
        events = [
            {'type': 'red_card', 'side': 'home', 'event_at_ms': 800, 'received_at_ms': 900},
            {'type': 'red_card', 'side': 'away', 'event_at_ms': 700, 'received_at_ms': 1100},
        ]
        self.assertEqual(len(visible_match_events(events, 1000)), 1)
        with_home_red = dynamic_poisson_distribution(1.5, 1.1, 1800, (0, 0), events, 1000)
        without_red = dynamic_poisson_distribution(1.5, 1.1, 1800, (0, 0), [], 1000)
        home_goals_with = sum(p for h, a, p in with_home_red if h > a)
        home_goals_without = sum(p for h, a, p in without_red if h > a)
        self.assertLess(home_goals_with, home_goals_without)
        malformed = [{'type': 'red_card', 'side': 'home', 'event_at_ms': 1}]
        with self.assertRaises(ValueError):
            visible_match_events(malformed, 1000)


if __name__ == '__main__':
    unittest.main()
