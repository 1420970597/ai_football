import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from tests.test_training_data import decision
from tools.repair_current_tail import repair_file


class CurrentTailRepairTests(unittest.TestCase):
    def test_rewrites_decision_tail_and_publishes_original_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'ledger' / 'ledger.jsonl'
            path.parent.mkdir()
            row = {'match_id': 'm', 'at': '2025-01-01T00:00:00+00:00', 'market': 'HAD',
                   'outcome': 'home', 'line': '0', 'is_live': False, 'is_pick': True,
                   'odds': 2.0, 'status': 'won', 'pnl': 1.0, 'ft_score': [0, 1],
                   'settlement_basis': 'full_score', 'entry_score': [0, 0]}
            path.write_text(json.dumps(row) + '\n')
            archive = Mock()
            out = repair_file(root, path, {'m': {'ft_score': [1, 0]}}, archive)
            revised = json.loads(path.read_text().splitlines()[0])
            self.assertEqual(revised['status'], 'won')
            self.assertEqual(revised['repair_status'], 'regraded')
            self.assertEqual(out['records'], 1)
            archive.revise.assert_called_once()

    def test_rewrites_replay_tail_as_gzip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'ledger' / 'live-replay.2025010100.jsonl.gz'
            path.parent.mkdir()
            row = {'match_id': 'm', 'model_version': 'old', 'score': [0, 0],
                   'evaluations': [], 'markets': [], 'computed_at': '2025-01-01T00:00:00+00:00'}
            with gzip.open(path, 'wt') as stream:
                stream.write(json.dumps(row) + '\n')
            archive = Mock()
            out = repair_file(root, path, {}, archive)
            with gzip.open(path, 'rt') as stream:
                revised = json.loads(next(stream))
            self.assertEqual(revised['repair_status'], 'no_derived_prediction')
            self.assertEqual(out['records'], 1)
            cursor = json.loads((root/'models'/'replay-cursor.json').read_text())
            self.assertEqual(cursor[path.name], path.stat().st_size)

    def test_repaired_tail_rebuilds_training_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'ledger'/'live-replay.2025010100.jsonl.gz'
            path.parent.mkdir(parents=True)
            with gzip.open(path, 'wt') as stream:
                stream.write(json.dumps(decision())+'\n')
            archive = Mock()
            out = repair_file(root, path, {}, archive)
            self.assertEqual(out['rebuilt_inputs'], 20)
            with sqlite3.connect(root/'models'/'inputs.sqlite3') as con:
                self.assertEqual(con.execute('SELECT count(*) FROM inputs').fetchone()[0], 20)
            cursor = json.loads((root/'models'/'replay-cursor.json').read_text())
            self.assertEqual(cursor[path.name], path.stat().st_size)


if __name__ == '__main__':
    unittest.main()
