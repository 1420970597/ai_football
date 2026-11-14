import unittest
from infra.monitor.independent_review import SYSTEM
from infra.monitor.consecutive_windows import evaluate


class SemanticOutcomeProtocolTests(unittest.TestCase):
    def test_source_gap_is_not_a_system_success_claim(self):
        self.assertIn('来源不完整本身不等于系统处理不明',SYSTEM)
        self.assertIn('系统未接受任何项',SYSTEM)
        self.assertIn('不得用模型原说明当缺项证据',SYSTEM)

    def test_missing_image_and_unexecuted_tools_remain_uncertain(self):
        self.assertIn('声明有图片而未提供',SYSTEM)
        self.assertIn('仍须uncertain',SYSTEM)
        self.assertIn('音频未转录必须uncertain',SYSTEM)

    def test_current_inventory_does_not_supply_a_missing_source_period(self):
        self.assertIn('库存只有全场', SYSTEM)
        self.assertIn('不能按现有全场报价记confirmed_omission', SYSTEM)
        self.assertIn('仍逐项按真实期间核验', SYSTEM)

    def test_protocol_does_not_relax_unknown_outcome_acceptance(self):
        passed,reasons=evaluate(dict(start=0,end=1800,upstream_complete=True,database_complete=True,
            source_health_ok=True,audit_complete=True,trusted_messages=1,recommendation_messages=1,
            missing_messages=0,unreviewed_messages=0,confirmed_omissions=0,confirmed_misbindings=0,
            duplicate_signals=0,deadline_violations=0,unexplained_outcomes=1,
            snapshot_sha256='frozen',audit_sha256='bound'))
        self.assertFalse(passed)
        self.assertEqual(['unexplained_outcomes'],reasons)
