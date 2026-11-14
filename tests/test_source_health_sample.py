import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime,timezone
import httpx
from infra.monitor.source_health_sample import summarize
from infra.monitor import source_health_sample


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


class PipelineHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_tool_readiness_is_required_even_when_all_tg_sources_healthy(self):
        factory = httpx.AsyncClient
        for outcome in ('ready', 'unavailable', 'malformed', 'network'):
            def handler(request):
                if request.url.path == '/status':
                    value = SourceHealthSampleTests().healthy()
                    value['source_health']['telegram']['last_success_at'] = datetime.now(timezone.utc).isoformat()
                    return httpx.Response(200, json=value)
                self.assertEqual('/readyz', request.url.path)
                self.assertNotIn('authorization', request.headers)
                if outcome == 'network':
                    raise httpx.ConnectError('isolated', request=request)
                if outcome == 'malformed':
                    return httpx.Response(200, text='not json')
                if outcome == 'unavailable':
                    return httpx.Response(503, json={'status':'unavailable'})
                return httpx.Response(200, json={'status':'ready','role':'mcp'})
            with patch.object(source_health_sample, 'load_settings', return_value=SimpleNamespace(collector_token='test-only')), patch.object(
                    source_health_sample.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
                value = await source_health_sample.sample()
            self.assertEqual(outcome == 'ready', value['ok'])
            self.assertEqual(outcome == 'ready', value['tool_api_ready'])
            self.assertEqual(outcome != 'ready', 'tool_api_not_ready' in value['reasons'])
