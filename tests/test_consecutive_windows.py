import unittest
from infra.monitor.consecutive_windows import evaluate, record, repaired


def good(start):
    return dict(start=start,end=start+1800,upstream_complete=True,database_complete=True,
                source_health_ok=True,audit_complete=True,trusted_messages=4,recommendation_messages=2,
                missing_messages=0,unreviewed_messages=0,confirmed_omissions=0,confirmed_misbindings=0,
                duplicate_signals=0,deadline_violations=0,unexplained_outcomes=0,
                snapshot_sha256=str(start),audit_sha256='audited')


class ConsecutiveWindowTests(unittest.TestCase):
    def test_three_contiguous_nonempty_audited_windows_complete(self):
        state={}
        for start in (1800,3600,5400):
            state=record(state,good(start))
        self.assertEqual(3,state['streak'])
        self.assertTrue(state['complete'])

    def test_failure_empty_or_incomplete_resets(self):
        for change in ({'trusted_messages':0},{'deadline_violations':1},
                       {'recommendation_messages':-1},{'upstream_complete':False},
                       {'unexplained_outcomes':1},{'audit_sha256':''}):
            with self.subTest(change=change):
                state=record({},good(1800));state=record(state,good(3600)|change)
                self.assertEqual(0,state['streak']);self.assertFalse(state['complete'])
                self.assertEqual(1,record(state,good(5400))['streak'])

    def test_nonempty_fully_audited_feedback_counts_without_forcing_new_picks(self):
        state = {}
        for start, count in ((1800, 2), (3600, 0), (5400, 0)):
            state = record(state, good(start) | {'recommendation_messages': count})
        self.assertEqual(3, state['streak'])
        self.assertTrue(state['complete'])

    def test_zero_picks_does_not_excuse_unknown_or_incomplete_review(self):
        for change in ({'unexplained_outcomes': 1}, {'audit_complete': False},
                       {'unreviewed_messages': 1}, {'missing_messages': 1},
                       {'confirmed_omissions': 1}, {'confirmed_misbindings': 1},
                       {'duplicate_signals': 1}, {'deadline_violations': 1},
                       {'source_health_ok': False}):
            with self.subTest(change=change):
                self.assertFalse(evaluate(good(1800) | {'recommendation_messages': 0} | change)[0])

    def test_recommendation_count_is_present_nonnegative_and_not_above_messages(self):
        for count in (None, True, -1, 5):
            self.assertIn('invalid_recommendation_count', evaluate(good(1800) | {'recommendation_messages': count})[1])

    def test_reassessment_uses_separate_state_and_keeps_legacy_history(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / 'infra/monitor/continuous_watch.py').read_text()
        self.assertIn("statefile=root/'acceptance-user-contract-v2.json'", source)
        self.assertIn("'previous_contract_state':'acceptance-state.json'", source)
        self.assertIn("'acceptance_contract':ACCEPTANCE_CONTRACT", source)

    def test_gap_and_repair_reset(self):
        state=record({},good(1800));state=record(state,good(5400))
        self.assertEqual(1,state['streak'])
        state=repaired(state,revision='fixed',at=8000)
        self.assertEqual(0,state['streak']);self.assertFalse(state['complete'])

    def test_duplicate_cannot_increase_or_reclassify_failed_window(self):
        first=good(1800)|{'deadline_violations':1};state=record({},first)
        self.assertEqual(state,record(state,good(1800)))
        with self.assertRaises(ValueError):
            record(state,good(1800)|{'snapshot_sha256':'changed'})

    def test_restart_preserves_streak_and_refuses_old_windows(self):
        import json
        state=json.loads(json.dumps(record({},good(3600))))
        self.assertEqual(2,record(state,good(5400))['streak'])
        with self.assertRaises(ValueError):record(state,good(1800))

    def test_window_straddling_repair_cannot_count(self):
        state=repaired({},revision='actual-live-repair',at=4000)
        state=record(state,good(3600))
        self.assertEqual(0,state['streak'])
        self.assertIn('repair_during_window',state['windows']['3600']['reasons'])
        self.assertEqual(1,record(state,good(5400))['streak'])
