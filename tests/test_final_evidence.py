import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from collector.leyu_client import parse_odds_block
from collector.leyu_realtime import RealtimeHub, ScoreStore


class FinalEvidenceTests(unittest.TestCase):
    def test_official_status_and_half_score_mapping(self):
        def match(status, period='0'):
            return parse_odds_block({'data':[{'mid':'m','ms':status,'mmp':period,
                 'msc':['S0|9:9','S1|2:1','S2|0:1']} ]})[0]
        self.assertFalse(match(110).is_finished)  # 即将开赛
        self.assertTrue(match(3).is_finished)
        self.assertTrue(match(1,'999').is_finished)
        self.assertEqual(match(3).half_score,(0,1))
        self.assertEqual(match(3).score,(2,1))

    def test_terminal_status_persists_full_and_half_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            hub=RealtimeHub(MagicMock(),resume=False)
            hub.scores_store=ScoreStore(str(Path(root)/'scores.json'))
            hub._handle_message({'cmd':'C103','cd':{'mid':'m','msc':['S1|2:1','S2|0:1']}})
            hub._handle_message({'cmd':'C102','cd':{'mid':'m','ms':3,'mmp':'999','mst':'5400'}})
            self.assertIn('m',hub.finished_mids())
            restored=RealtimeHub(MagicMock(),resume=False)
            restored.scores_store=hub.scores_store
            restored._load_scores()
            self.assertEqual(restored.scores_snapshot()['m'],(2,1))
            self.assertEqual(restored.half_score('m'),(0,1))
            self.assertIn('m',restored.finished_mids())

    def test_legacy_and_removal_are_not_final_proof(self):
        with tempfile.TemporaryDirectory() as root:
            hub=RealtimeHub(MagicMock(),resume=False)
            hub.scores_store=ScoreStore(str(Path(root)/'scores.json'))
            hub.scores_store.save({'m':(0,0)},['m'],force=True)
            hub._load_scores()
            self.assertEqual(hub.finished_mids(),[])
            hub._handle_message({'cmd':'C109','cd':[{'mid':'m','ms':110}]})
            self.assertEqual(hub.finished_mids(),[])
            hub._handle_message({'cmd':'C109','cd':[{'mid':'m','ms':3,'msc':['S1|2:0']}]})
            self.assertEqual(hub.finished_mids(),['m'])
            self.assertEqual(hub.scores_snapshot()['m'],(2,0))
