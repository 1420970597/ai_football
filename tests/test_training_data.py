import json
from pathlib import Path
import tempfile
import unittest
import sqlite3
from datetime import datetime

from core.learned_model import DATA_VERSION, FEATURES
from core.live_model import MODEL_VERSION
from store.ledger_db import LedgerDB
from store.training_data import dataset, snapshot_inputs, write_inputs


def decision():
    common = {'p_market': .5, 'p_model': .6, 'ts_ms': 1000, 'quote_received_at_ms': 1000, 'research_only': False,
              'decision_status': 'settleable', 'settlement_basis': 'full_score', 'training_history': [[100, 2], [900, 2.1], [2000, 9]], 'trend_pct': 350}
    candidates = [{**common, 'market': 'HAD', 'line': '', 'outcome': 'home'},
                  {**common, 'market': 'OU', 'line': '2/2.5', 'outcome': 'over'},
                  {**common, 'settlement_basis': 'remaining_score', 'market': 'AH', 'line': '-0.5', 'outcome': 'away'},
                  {**common, 'settlement_basis': 'half_score_required', 'market': 'HAD_1H', 'line': '', 'outcome': 'home'}]
    return {'model_version': MODEL_VERSION, 'competition_type': 'real', 'match_id': 'm1', 'computed_at': '2025-01-01T12:00:00+00:00',
            'score': [0,0], 'elapsed_s': 1200, 'input_cutoff_ms': 1000, 'received_at_ms': 1000,
            'evaluations': [{'algorithm': a, 'candidates': candidates} for a in ('poisson_market', 'poisson_time_decay')]}


class TrainingDataTests(unittest.TestCase):
    def test_label_revisions_are_append_only_and_latest_available_score_is_selected(self):
        from store.training_data import labels
        with tempfile.TemporaryDirectory() as directory:
            db = LedgerDB(Path(directory) / 'ledger.sqlite3')
            first = {'match_id': 'm1', 'decision_id': 'd1', 'at': '2025-01-01T12:00:00+00:00',
                     'settled_at': '2025-01-01T14:00:00+00:00',
                     'label_available_at': '2025-01-01T14:00:00+00:00', 'updated_at': '2025-01-01T14:00:00+00:00',
                     'competition_type': 'real', 'status': 'won', 'ft_score': [1, 0], 'ht_score': [1, 0]}
            db.put([first], lambda _: 1.0)
            db.connection.commit()
            revised = {**first, 'settled_at': '2025-01-01T15:00:00+00:00',
                       'label_available_at': '2025-01-01T15:00:00+00:00',
                       'updated_at': '2025-01-01T15:00:00+00:00', 'ft_score': [1, 1], 'ht_score': [0, 0]}
            db.put([revised], lambda _: 2.0)
            db.connection.commit()
            revisions = db.connection.execute('SELECT count(*) FROM label_revisions').fetchone()[0]
            self.assertEqual(revisions, 2)
            self.assertEqual(labels(db.connection)['m1']['ft_score'], (1, 1))

    def test_training_sample_is_bounded_reproducible_and_result_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = LedgerDB(root/'ledger.sqlite3')
            final = {'match_id':'m1', 'at':'2025-01-01T12:00:00+00:00',
                     'settled_at':'2025-01-01T14:00:00+00:00', 'competition_type':'real',
                     'status':'won', 'ft_score':[2,1], 'ht_score':[1,0], 'label_available_at':'2025-01-01T14:00:00+00:00'}
            db.put([final], lambda _:1.0)
            db.connection.commit()
            path = root/'inputs.sqlite3'
            write_inputs(path, snapshot_inputs(decision()))
            quality = {}
            a = dataset(db.path,path,max_per_match=3,audit=quality)
            b = dataset(db.path,path,max_per_match=3)
            self.assertEqual(a,b)
            self.assertEqual(len(a),3)
            self.assertEqual(quality['source_eligible_decisions'],8)
            final['ft_score'] = [0,3]
            final['ht_score'] = [0,1]
            db.put([final],lambda _:2.0)
            db.connection.commit()
            c = dataset(db.path,path,max_per_match=3)
            self.assertEqual([r['x'] for r in a],[r['x'] for r in c])
            with self.assertRaises(ValueError):
                dataset(db.path,path,max_per_match=0)

    def test_old_features_are_quarantined_without_relabeling_as_clean(self):
        old = decision()
        old['model_version'] = 'live-poisson-market-v1'
        self.assertEqual(snapshot_inputs(old), [])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = LedgerDB(root/'ledger.sqlite3')
            path = root/'inputs.sqlite3'
            rows = snapshot_inputs(decision())
            write_inputs(path, rows)
            with sqlite3.connect(path) as con:
                con.execute('UPDATE inputs SET data_version=NULL,entry_score=NULL')
            quality = {}
            self.assertEqual(dataset(db.path,path,audit=quality), [])
            self.assertEqual(quality['quarantined_inputs'],8)

    def test_remaining_labels_require_benchmark_and_weight_half_stake(self):
        from store.training_data import target
        final = {'ft_score':[1,0],'ht_score':[1,0]}
        row = {'market':'AH','outcome':'home','line':'-0/0.5',
               'entry_score':'[1,0]','settlement_basis':'remaining_score'}
        self.assertEqual(target(row,final),(0,.5))
        self.assertEqual(target({**row,'market':'AH_1H'},final),(0,.5))
        self.assertIsNone(target({**row,'settlement_basis':'full_score'},final))
        self.assertIsNone(target({**row,'entry_score':None},final))
        self.assertIsNone(target({**row,'entry_score':'[2,0]'},final))
        self.assertEqual(snapshot_inputs(decision())[0]['data_version'],DATA_VERSION)

    def test_ou_and_ah_labels_follow_asian_line_payment_weights(self):
        from store.training_data import target
        final = {'ft_score': [1, 0], 'ht_score': [1, 0]}
        base = {'entry_score': [0, 0]}
        cases = (
            ({**base, 'market': 'OU', 'outcome': 'over', 'line': '2/2.5',
              'settlement_basis': 'full_score'}, (0, 1.0)),
            ({**base, 'market': 'OU', 'outcome': 'under', 'line': '1/1.5',
              'settlement_basis': 'full_score'}, (1, .5)),
            ({**base, 'market': 'AH', 'outcome': 'home', 'line': '-0/0.5',
              'settlement_basis': 'remaining_score'}, (1, 1.0)),
            ({**base, 'entry_score': [1, 0], 'market': 'AH', 'outcome': 'away', 'line': '0/0.5',
              'settlement_basis': 'remaining_score'}, (0, .5)),
        )
        for row, expected in cases:
            with self.subTest(row=row):
                self.assertEqual(target(row, final), expected)

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

    def test_late_received_history_is_excluded_from_offline_features(self):
        source = decision()
        # Event time is before cutoff, but the quote was first received after it.
        source['evaluations'][0]['candidates'][0]['training_history'] = [
            [100, 2.0, 900], [950, 2.2, 950], [990, 3.0, 1100]]
        source['evaluations'][0]['candidates'][0]['quote_received_at_ms'] = 900
        rows = snapshot_inputs(source)
        row = next(r for r in rows if r['algorithm'] == 'poisson_market' and r['market'] == 'HAD')
        x = json.loads(row['x'])
        self.assertAlmostEqual(x[FEATURES.index('trend_return')], 0.1)
        self.assertEqual(x[FEATURES.index('trend_points')], 2 / 40)

    def test_missing_or_late_dual_time_evidence_is_quarantined(self):
        source = decision()
        source.pop('received_at_ms')
        self.assertEqual(snapshot_inputs(source), [])
        source = decision()
        source['evaluations'][0]['candidates'][0]['quote_received_at_ms'] = 1001
        rows = snapshot_inputs(source)
        self.assertEqual(len(rows), 6)

    def test_ledger_fallback_cannot_bypass_dual_time_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = LedgerDB(root / 'ledger.sqlite3')
            final = {'match_id':'m1', 'at':'2025-01-01T12:00:00+00:00',
                     'settled_at':'2025-01-01T14:00:00+00:00', 'competition_type':'real',
                     'status':'won', 'ft_score':[2,1], 'ht_score':[1,0],
                     'label_available_at':'2025-01-01T14:00:00+00:00'}
            source = decision()
            payload = {'match_id':'m1', 'at':source['computed_at'], 'algorithm':'poisson_market',
                       'market':'HAD', 'line':'', 'outcome':'home', 'status':'won',
                       'competition_type':'real', 'trigger':'live_forecast', 'research_only':0,
                       'model_version':MODEL_VERSION, 'p_market':.5, 'settled_at':final['settled_at']}
            db.put([final, payload], lambda _:1.0)
            db.connection.commit()
            # No certified input snapshot exists. A plausible ledger payload and
            # matching label must remain quarantined instead of rebuilding features.
            with sqlite3.connect(root / 'inputs.sqlite3') as inputs:
                inputs.execute('CREATE TABLE inputs (match_id TEXT, at TEXT, data_version TEXT, '
                               'model_version TEXT, feature_version TEXT)')
            quality = {}
            self.assertEqual(dataset(db.path, root / 'inputs.sqlite3', audit=quality), [])
            self.assertEqual(quality['quarantined_ledger'], 1)

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
                     'competition_type': 'real', 'status': 'won', 'ft_score': [2,1], 'ht_score': [1,0],
                     'label_available_at': '2025-01-01T14:00:00+00:00'}
            db.put([final], lambda value: datetime.fromisoformat(value).timestamp())
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
            final['label_available_at'] = '2025-01-01T14:30:00+00:00'
            db.put([final], lambda value: datetime.fromisoformat(value).timestamp())
            db.connection.commit()
            self.assertEqual(len(dataset(db.path, path)), 6)
            final['settled_at'] = '2025-01-01T11:00:00+00:00'
            final['label_available_at'] = '2025-01-01T11:00:00+00:00'
            db.put([final], lambda value: datetime.fromisoformat(value).timestamp())
            db.connection.commit()
            # Append-only labels retain the latest actually available revision;
            # a later-arriving backdated correction cannot erase it.
            self.assertEqual(len(dataset(db.path, path)), 6)
