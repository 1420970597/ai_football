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

    def test_failure_empty_incomplete_or_ad_only_resets(self):
        for change in ({'trusted_messages':0},{'deadline_violations':1},
                       {'recommendation_messages':0},{'upstream_complete':False},
                       {'unexplained_outcomes':1},{'audit_sha256':''}):
            with self.subTest(change=change):
                state=record({},good(1800));state=record(state,good(3600)|change)
                self.assertEqual(0,state['streak']);self.assertFalse(state['complete'])
                self.assertEqual(1,record(state,good(5400))['streak'])

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
