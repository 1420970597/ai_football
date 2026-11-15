import unittest
from infra.monitor.review_diagnosis_watch import summarize

class DiagnosisWatchTests(unittest.TestCase):
    def test_private_refs_counts_and_incomplete_audit_remain_honest(self):
        result=summarize([{'id':'private-message','state':'unresolved',
            'last_error':'detection_deadline_exceeded','accepted_signal_count':1,'raw_text':'SECRET TEXT',
            'queue_ms':10,'first_tool_ms':9000,'prompt_tokens':22000},
            {'id':'other','state':'unresolved','has_model_result':True,
             'summary':{'completion_verified':True}}])
        self.assertEqual(2,result['messages'])
        self.assertEqual(1,result['diagnosis']['deadline_with_signals'])
        self.assertEqual(1,result['diagnosis']['model_concluded_without_signal'])
        self.assertFalse(result['semantic_omission_audit_complete'])
        self.assertEqual(0,result['business_writes'])
        self.assertNotIn('SECRET TEXT',str(result))
        self.assertNotIn('private-message',str(result))

    def test_empty_window_has_no_fabricated_latency_or_pass(self):
        result=summarize([])
        self.assertEqual(0,result['messages'])
        self.assertEqual({},result['timing'])
        self.assertFalse(result['semantic_omission_audit_complete'])
