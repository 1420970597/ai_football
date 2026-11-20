"""Preserve native MCP evidence while removing only JSON indentation."""
import json
import unittest

import httpx
from mcp.types import CallToolResult, ImageContent, TextContent

import test_mcp_transport as support


class CompactToolTransportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.MCPTransportTests.asyncSetUp
    asyncTearDown = support.MCPTransportTests.asyncTearDown
    rpc = support.MCPTransportTests.rpc

    def compact(self, result):
        function = getattr(self.server, 'compact_tool_result', None)
        self.assertTrue(callable(function), 'project adapter must expose its lossless native-result formatter')
        return function(result)

    def result(self, value, *, text=None, **kwargs):
        content = TextContent(type='text', text=json.dumps(value, ensure_ascii=False, indent=2) if text is None else text)
        return CallToolResult(content=[content], structured_content=value, **kwargs)

    async def test_real_rpc_emits_compact_text_with_same_structured_values_and_context(self):
        native = {'items': [{'id': 'leyu:verified', 'home_team': '水晶宫',
            'price': '1.95000', 'line': '+1/1.5', 'evidence': '引号"与换行\n保留',
            'active': True, 'score': None}], 'next_cursor': None, 'search_complete': True}
        captured = []

        def upstream(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=native)

        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        response = await self.rpc('tools/call', {'name': 'search_matches', 'arguments': {
            'review_id': 'job', 'attempt_id': 'attempt', 'team_query': '水晶宫'}})
        self.assertEqual(200, response.status_code)
        result = response.json()['result']
        self.assertFalse(result.get('isError', False))
        self.assertNotIn('structuredContent', result, 'bare-dict native tools do not publish a structured result')
        self.assertEqual(native, json.loads(result['content'][0]['text']))
        self.assertEqual(json.dumps(native, ensure_ascii=False, separators=(',', ':')), result['content'][0]['text'])
        self.assertEqual(1, len(captured))
        self.assertEqual(('job', 'attempt'), (captured[0]['review_id'], captured[0]['attempt_id']))

    async def test_native_output_schema_and_four_tool_allowlist_are_unchanged(self):
        response = await self.rpc('tools/list')
        tools = response.json()['result']['tools']
        self.assertEqual({'search_matches','resolve_match','list_match_markets','record_expert_signals'},
                         {tool['name'] for tool in tools})
        for tool in tools:
            self.assertNotIn('outputSchema', tool, 'preserve the native bare-dict output contract')
            self.assertIn('attempt_id', tool['inputSchema']['required'])

    async def test_rpc_error_remains_error_and_makes_only_one_business_request(self):
        captured = []

        def upstream(request):
            captured.append(request.url.path)
            return httpx.Response(409, json={'detail': '租约已失效'})

        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        response = await self.rpc('tools/call', {'name': 'list_match_markets', 'arguments': {
            'review_id':'job', 'attempt_id':'attempt', 'fixture_id':'leyu:verified'}})
        result = response.json()['result']
        self.assertTrue(result['isError'])
        self.assertIn('租约已失效', result['content'][0]['text'])
        self.assertEqual(1, len(captured))

    async def test_success_preserves_native_metadata_and_text_annotations(self):
        value = {'items': [{'price':'2.00000','line':'-0.5'}], 'count': 1}
        original = self.result(value, _meta={'audit':{'version':3}})
        original.content[0].annotations = {'audience': ['assistant'], 'priority': .5}
        result = self.compact(original)
        self.assertEqual(original.structured_content, result.structured_content)
        self.assertEqual(original.meta, result.meta)
        self.assertEqual(original.content[0].annotations, result.content[0].annotations)
        self.assertEqual(json.dumps(value, separators=(',', ':')), result.content[0].text)
        self.assertIn('\n', original.content[0].text, 'formatting must not mutate original native result')

    async def test_error_native_result_is_returned_verbatim(self):
        original = self.result({'detail':'不允许'}, is_error=True)
        self.assertIs(original, self.compact(original))

    async def test_mismatched_text_and_structured_evidence_are_not_rewritten(self):
        original = self.result({'phase':'live'}, text='{"phase": "scheduled"}')
        self.assertIs(original, self.compact(original))

    async def test_non_json_or_scalar_text_is_not_rewritten(self):
        for text in ('专家原文 {不是JSON}', '"专家原文"', '42', '[1, 2]'):
            with self.subTest(text=text):
                original = self.result({'content':'原生'}, text=text)
                self.assertIs(original, self.compact(original))

    async def test_mixed_content_and_plain_unstructured_result_are_not_rewritten(self):
        original = self.result({'count': 1})
        original.content.append(ImageContent(type='image', data='AA==', mime_type='image/png'))
        self.assertIs(original, self.compact(original))
        unstructured = CallToolResult(content=[TextContent(type='text', text='未解决原文')])
        self.assertIs(unstructured, self.compact(unstructured))

    async def test_json_looking_source_strings_keep_their_exact_whitespace(self):
        source = '{\n  "原文": "  +1/1.5  "\n}'
        value = {'source': source, 'empty':{}, 'array':[], 'flag':False}
        result = self.compact(self.result(value))
        self.assertEqual(source, json.loads(result.content[0].text)['source'])
        self.assertEqual(value, result.structured_content)

    async def test_already_compact_native_result_is_returned_verbatim(self):
        value = {'phase':'live', 'line':'1.25000'}
        original = self.result(value, text=json.dumps(value, separators=(',', ':')))
        self.assertIs(original, self.compact(original))

    async def test_other_native_protocol_result_is_returned_verbatim(self):
        original = object()
        self.assertIs(original, self.compact(original))
