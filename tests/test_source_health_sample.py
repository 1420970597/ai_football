import json
import unittest
from infra.monitor.source_health_sample import summarize


class SourceHealthSampleTests(unittest.TestCase):
    def healthy(self):
        return {'connected': True, 'authorized': True,
                'source_runtime': {'private-chat': {}, 'second-chat': {}},
                'source_health': {'telegram': {'state': 'connected', 'degraded': False,
                    'source_states': {'private-chat': 'caught_up', 'second-chat': 'catching_up'},
                    'source_errors': {}, 'last_success_at': '1970-01-01T00:01:30+00:00'}},
                'live_event_delivery': {'received': 5, 'staged': 2, 'deferred': 0}}

    def test_healthy_counts_do_not_export_source_identity(self):
        result = summarize(self.healthy(), at=100)
        self.assertTrue(result['ok'])
        self.assertEqual(2, result['configured_sources'])
        self.assertNotIn('private-chat', json.dumps(result))

    def test_missing_source_and_recovery_failure_are_visible(self):
        value = self.healthy()
        value['source_health']['telegram']['source_states'] = {'private-chat': 'unavailable'}
        result = summarize(value, at=100)
        self.assertFalse(result['ok'])
        self.assertIn('source_coverage_incomplete', result['reasons'])
        self.assertIn('source_work_deferred', result['reasons'])

    def test_connected_alone_does_not_prove_health(self):
        result = summarize({'connected': True, 'authorized': False}, at=100)
        self.assertFalse(result['ok'])
        self.assertIn('unauthorized', result['reasons'])

    def test_old_success_does_not_hide_stalled_collection(self):
        result = summarize(self.healthy(), at=300)
        self.assertFalse(result['ok'])
        self.assertIn('source_health_stale', result['reasons'])
