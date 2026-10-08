import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from service.ledger import DecisionLedger, LedgerEntry

SPEC = importlib.util.spec_from_file_location('repair_ledger', Path(__file__).parents[1] / 'tools/repair_ledger.py')
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RepairLedgerTests(unittest.TestCase):
    def test_confirmed_results_only_and_repeat_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            led = DecisionLedger(root)
            led._append([LedgerEntry(at='2026-01-01', match_id='yes', market='OU', line='2.25',
                                     outcome='over', odds=2, is_pick=True, status='lost', pnl=-1),
                         LedgerEntry(at='2026-01-01', match_id='no', market='HAD', outcome='draw', odds=2, is_pick=True)])
            scores = root / 'scores.json'
            scores.write_text(json.dumps({'scores': {'yes': {'ft': [1, 1], 'done': True},
                                                      'no': {'ft': [0, 0], 'done': False}}}))
            result = MODULE.repair(root, scores)
            self.assertEqual(result['after']['profit_units'], -.5)
            self.assertEqual(result['after']['pending'], 1)
            self.assertEqual(MODULE.repair(root, scores)['after'], result['after'])
