"""Run inside the pinned Hermes image; no provider, database, or business calls."""
import json
import unittest

from mcp.types import CallToolResult, TextContent
from tools.mcp_tool_handlers import _render_call_tool_result


class ComposedErrorRenderingTests(unittest.TestCase):
    def receipt(self, error):
        return {'completed_receipt': {
            'review_id': '8536d46f-1e73-4662-824a-90b3689b3658',
            'attempt_id': '6840c933-f380-4ef4-b10d-cb9f1971b0cb',
            'search_key': 'a'*64, 'search_complete': True, 'next_cursor': None,
            'resolution': {'resolved': True, 'fixture': {
                'id': 'isolated-fixture', 'realtime_eligible': True}}},
            'failed_operation': 'list_match_markets', 'market_read_succeeded': False,
            'error': error}

    def render(self, payload):
        result = CallToolResult(isError=True, content=[TextContent(type='text',
            text='Error executing tool search_matches: '+json.dumps(payload, ensure_ascii=False))])
        model_error = json.loads(_render_call_tool_result(result, 'football'))['error']
        return model_error, json.loads(model_error[model_error.index('{'):])

    def test_protocol_failure_keeps_exact_attempt_and_search_receipt_for_model(self):
        expected = self.receipt('review attempt is no longer active')
        _, actual = self.render(expected)
        self.assertEqual(expected, actual)
        self.assertFalse(actual['market_read_succeeded'])
        self.assertNotIn('market_result', actual)
        self.assertNotIn('quote_id', json.dumps(actual))

    def test_fixed_framework_redacts_error_credentials_without_losing_receipt(self):
        canary = 'isolated-rendering-canary'
        expected = self.receipt('Bearer '+canary+' followed by unavailable upstream')
        text, actual = self.render(expected)
        self.assertNotIn(canary, text)
        self.assertIn('[REDACTED]', text)
        self.assertEqual(expected['completed_receipt'], actual['completed_receipt'])
        self.assertFalse(actual['market_read_succeeded'])


if __name__ == '__main__':
    unittest.main()
