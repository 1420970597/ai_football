import copy
from dataclasses import asdict, replace
import unittest

from core.live_model import MODEL_VERSION
from service.history_repair import merge_final_evidence, repair_grade, repair_replay
from service.live_expert import LiveExpertService
from service.runtime_settings import ALGORITHMS, RuntimeConfig
from store.training_data import snapshot_inputs
from tests import test_live_expert


class HistoryRepairTests(unittest.TestCase):
    def test_score_revisions_follow_event_time_and_changed_result_availability(self):
        events = [{'match_id':'m','at':'2025-01-01T14:00:00+00:00','done':True,'ft':[2,0]},
                  {'match_id':'m','at':'2025-01-01T12:00:00+00:00','done':True,'ft':[1,0]},
                  {'match_id':'m','at':'2025-01-01T14:01:00+00:00','done':True,'ft':[2,0]}]
        finals = merge_final_evidence({'m':{'ft_score':[1,0],'settled_at':events[1]['at']}},events)
        self.assertEqual(finals['m']['ft_score'],[2,0])
        self.assertEqual(finals['m']['settled_at'],events[0]['at'])
        row = {'match_id':'m','market':'HAD','outcome':'home','line':'','settlement_basis':'full_score',
               'ft_score':[1,0],'settled_at':events[1]['at']}
        out = repair_grade(row,finals)
        self.assertEqual(out['settled_at'],events[0]['at'])
        self.assertEqual(out['ft_score'],[2,0])

    def test_unverified_old_label_is_excluded_and_preserved_as_evidence(self):
        row = {'match_id':'m','market':'AH','outcome':'home','line':'-0.5','entry_score':None,
               'model_version':'live-poisson-market-v1','is_live':True,'status':'won','pnl':1,'odds':2,'is_pick':True}
        out = repair_grade(row,{'m':{'ft_score':[2,0]}})
        self.assertEqual(out['status'],'void')
        self.assertEqual(out['original_status'],'won')
        self.assertEqual(out['original_pnl'],1)
        self.assertEqual(out['pnl'],0)

    def frozen(self):
        hub = test_live_expert.LiveExpertTests().hub()
        service = LiveExpertService()
        cfg = replace(RuntimeConfig(), algorithms=tuple(ALGORITHMS))
        service.configure(cfg, 1)
        snapshot = hub.decision_snapshot('m')
        row = service.compute(snapshot, hub)
        row.update(model_version='live-poisson-market-v1', schema_version=3, config=asdict(cfg), received_at_ms=row['input_cutoff_ms'])
        for evaluation in row['evaluations']:
            for candidate in evaluation['candidates']:
                candidate['quote_received_at_ms'] = row['input_cutoff_ms']
        return row

    def test_rebuild_uses_frozen_peers_and_preserves_cutoff(self):
        row = self.frozen()
        original = copy.deepcopy(row)
        for e in row['evaluations']:
            if e['algorithm'] == 'economics_risk_adjusted':
                for c in e['candidates']:
                    c['p_model'] = 1
        repaired = repair_replay(row, {})
        self.assertEqual(repaired['repair_status'], 'rebuilt_frozen_inputs')
        self.assertEqual(repaired['input_cutoff_ms'], original['input_cutoff_ms'])
        self.assertEqual(repaired['computed_at'], original['computed_at'])
        self.assertEqual(repaired['model_version'], MODEL_VERSION)
        self.assertTrue(snapshot_inputs(repaired))
        risk = next(e for e in repaired['evaluations'] if e['algorithm'] == 'economics_risk_adjusted')
        self.assertTrue(all(0 < c['p_model'] < 1 for c in risk['candidates']))
        self.assertEqual(repair_replay(repaired, {})['evaluations'], repaired['evaluations'])

    def test_rebuild_quarantines_invalid_decision_receive_clock(self):
        row = self.frozen()
        row['received_at_ms'] = row['input_cutoff_ms'] + 1
        repaired = repair_replay(row, {})
        self.assertFalse(repaired['training_eligible'])
        self.assertEqual(snapshot_inputs(repaired), [])

    def test_missing_distribution_is_explicit_not_certified(self):
        row = {'match_id':'m', 'model_version':'live-poisson-market-v1', 'score':[1,0],
               'candidates':[{'market':'AH', 'line':'-0/0.5', 'outcome':'home', 'p_model':.8}]}
        out = repair_replay(row, {'m':{'ft_score':[1,0]}})
        self.assertFalse(out['training_eligible'])
        c = out['candidates'][0]
        self.assertNotIn('p_model', c)
        self.assertEqual(c['original_p_model'], .8)
        self.assertEqual(c['status'], 'half_lost')
        self.assertEqual(snapshot_inputs(out), [])

    def test_regrade_does_not_move_result_knowledge_time(self):
        row = {'match_id':'m','market':'AH','line':'-0/0.5','outcome':'home',
               'model_version':'live-poisson-market-v1','is_live':True,'entry_score':[1,0],
               'settlement_basis':'full_score','settled_at':'2025-01-01','odds':1.9,'is_pick':True}
        out = repair_grade(row, {'m':{'ft_score':[1,0]}})
        self.assertEqual(out['status'], 'half_lost')
        self.assertEqual(out['pnl'], -.5)
        self.assertEqual(out['settled_at'], row['settled_at'])
        self.assertEqual(repair_grade(row, {})['repair_status'], 'missing_final_score')
