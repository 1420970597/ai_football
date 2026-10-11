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
    def test_live_basis_repaired_but_input_version_and_result_time_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            led = DecisionLedger(root)
            led._append([LedgerEntry(at='2026-01-01T12:00:00+00:00', match_id='m', market='AH',
                         line='-0/0.5',outcome='home',odds=1.49,is_pick=True,is_live=True,
                         entry_score=[1,0],settlement_basis='full_score',model_version='live-poisson-market-v1',
                         status='won',pnl=.49,ft_score=[1,0],settled_at='2026-01-01T14:00:00+00:00')])
            scores = root/'scores.json'
            scores.write_text(json.dumps({'scores':{'m':{'ft':[1,0],'done':True}}}))
            result = MODULE.repair(root,scores)
            self.assertEqual(result['migrated_live_handicap_basis'],1)
            row = led.load()[0]
            self.assertEqual(row.status,'half_lost')
            self.assertEqual(row.settled_at,'2026-01-01T14:00:00+00:00')
            self.assertEqual(row.model_version,'live-poisson-market-v1')
            self.assertEqual(MODULE.repair(root,scores)['migrated_live_handicap_basis'],0)

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
