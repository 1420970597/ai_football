"""TASK-61: the learner stacks algorithm disagreements on a market offset."""
import math
import random
import unittest
from datetime import datetime, timedelta, timezone

from core.learned_model import FEATURES, STACK_INDICES, features, fit_weights, predict


def row(i, market, peer, y):
    at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i)
    source = {'market': 'OU', 'outcome': 'over', 'line': '2.5', 'algorithm': 'poisson_time_decay',
              'entry_score': [0, 0], 'entry_clock_s': 1200, 'p_market': market, 'p_fused': peer,
              'algorithm_probabilities': {'poisson_market': peer, 'poisson_time_decay': peer,
                                          'devig_consensus': market, 'economics_risk_adjusted': market,
                                          'microstructure_adjusted': market}}
    return {'match_id': str(i), 'at': at.isoformat(), 'settled_at': (at + timedelta(hours=2)).isoformat(),
            'x': features(source), 'baseline': market, 'y': y}


class StackedLearnerTests(unittest.TestCase):
    def test_only_stack_coordinates_move(self):
        rng = random.Random(1)
        rows = [row(i, .5, rng.choice((.4, .6)), rng.randint(0, 1)) for i in range(200)]
        weights = fit_weights(rows)
        self.assertEqual(len(weights), len(FEATURES))
        for i, w in enumerate(weights):
            if i not in STACK_INDICES:
                self.assertEqual(w, 0.0, FEATURES[i])

    def test_recovers_informative_disagreement_and_beats_market(self):
        # Truth sits halfway between market and the Poisson peers: the stack
        # must learn a positive weight on that disagreement.
        rng = random.Random(7)
        rows = []
        for i in range(1500):
            market = rng.uniform(.3, .7)
            peer = min(.95, max(.05, market + rng.uniform(-.2, .2)))
            truth = (market + peer) / 2
            rows.append(row(i, market, peer, int(rng.random() < truth)))
        train, test = rows[:1000], rows[1000:]
        weights = fit_weights(train)
        peer_index = FEATURES.index('peer_poisson_market')
        self.assertGreater(weights[peer_index] + weights[FEATURES.index('peer_poisson_time_decay')], .2)
        model = sum((predict(weights, r['x']) - r['y']) ** 2 for r in test)
        market = sum((r['baseline'] - r['y']) ** 2 for r in test)
        self.assertLess(model, market)

    def test_uninformative_peers_stay_near_market(self):
        rng = random.Random(3)
        rows = []
        for i in range(1500):
            market = rng.uniform(.3, .7)
            peer = min(.95, max(.05, market + rng.uniform(-.2, .2)))
            rows.append(row(i, market, peer, int(rng.random() < market)))
        weights = fit_weights(rows)
        deviation = [abs(predict(weights, r['x']) - r['baseline']) for r in rows]
        self.assertLess(sum(deviation) / len(deviation), .03)
        self.assertLess(max(abs(weights[i]) for i in STACK_INDICES), .15)

    def test_zero_weights_equal_market(self):
        r = row(0, .55, .7, 1)
        self.assertTrue(math.isclose(predict([0.0] * len(FEATURES), r['x']), .55, rel_tol=1e-4))


if __name__ == '__main__':
    unittest.main()
