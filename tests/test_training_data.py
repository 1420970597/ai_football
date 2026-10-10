import json
from pathlib import Path
import tempfile
import unittest

from core.learned_model import FEATURES
from store.ledger_db import LedgerDB
from store.training_data import dataset, snapshot_inputs, write_inputs


def decision():
    common = {'p_market': .5, 'p_model': .6, 'ts_ms': 1000, 'research_only': False,
              'decision_status': 'settleable', 'training_history': [[100, 2], [900, 2.1], [2000, 9]], 'trend_pct': 350}
    candidates = [{**common, 'market': 'HAD', 'line': '', 'outcome': 'home'},
                  {**common, 'market': 'OU', 'line': '2/2.5', 'outcome': 'over'},
                  {**common, 'market': 'AH', 'line': '-0.5', 'outcome': 'away'},
                  {**common, 'market': 'HAD_1H', 'line': '', 'outcome': 'home'}]
    return {'competition_type': 'real', 'match_id': 'm1', 'computed_at': '2025-01-01T12:00:00+00:00',
            'score': [0,0], 'elapsed_s': 1200, 'input_cutoff_ms': 1000,
            'evaluations': [{'algorithm': a, 'candidates': candidates} for a in ('poisson_market', 'poisson_time_decay')]}


class TrainingDataTests(unittest.TestCase):
    def test_all_markets_algorithms_and_strict_prefix(self):
        rows = snapshot_inputs(decision())
        self.assertEqual(len(rows), 8)
        for row in rows:
            x = json.loads(row['x'])
            self.assertAlmostEqual(x[FEATURES.index('trend_return')], .05)
            self.assertEqual(x[FEATURES.index('trend_points')], 2/40)
        source = decision()
        source['evaluations'][0]['candidates'][0]['ts_ms'] = 1001
        self.assertEqual(len(snapshot_inputs(source)), 6)  # candidate list shared by two test algorithms
        source.pop('input_cutoff_ms')
        self.assertEqual(snapshot_inputs(source), [])

    def test_same_snapshot_peer_probabilities_are_combined(self):
        source = decision()
        source['evaluations'][1]['candidates'] = [{**c,'p_model':.8} for c in source['evaluations'][1]['candidates']]
        rows=snapshot_inputs(source)
        x=json.loads(rows[0]['x'])
        self.assertGreater(x[FEATURES.index('peer_poisson_time_decay')],x[FEATURES.index('peer_poisson_market')])
        self.assertAlmostEqual(x[FEATURES.index('peer_missing')],.6)

    def test_new_samples_settle_each_market_half_scores_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = LedgerDB(root / 'ledger.sqlite3')
            final = {'match_id': 'm1', 'at': '2025-01-01T12:00:00+00:00', 'settled_at': '2025-01-01T14:00:00+00:00',
                     'competition_type': 'real', 'status': 'won', 'ft_score': [2,1], 'ht_score': [1,0]}
            db.put([final], lambda _: 1.0)
            db.connection.commit()
            path = root / 'inputs.sqlite3'
            write_inputs(path, snapshot_inputs(decision()))
            rows = dataset(db.path, path)
            self.assertEqual(len(rows), 8)
            self.assertEqual(len({r['match_id'] for r in rows}), 1)
            self.assertEqual({r['y'] for r in rows}, {0,1})
            write_inputs(path, snapshot_inputs(decision()))
            self.assertEqual(len(dataset(db.path, path)), 8)
            final.pop('ht_score')
            db.put([final], lambda _: 2.0)
            db.connection.commit()
            self.assertEqual(len(dataset(db.path, path)), 6)
            final['settled_at'] = '2025-01-01T11:00:00+00:00'
            db.put([final], lambda _: 3.0)
            db.connection.commit()
            self.assertEqual(dataset(db.path, path), [])
