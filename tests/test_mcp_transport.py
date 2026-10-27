"""真实 MCP SDK 流式 HTTP 协议验证；使用隔离假业务响应。"""
import importlib.util
import asyncio
import os
import sys
from pathlib import Path
import unittest

import httpx


class MCPTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        os.environ['EXPERT_MCP_TOKEN'] = 'isolated-mcp-transport'
        path = Path(__file__).resolve().parents[1] / 'services/hermes-mcp/server.py'
        spec = importlib.util.spec_from_file_location('isolated_mcp_server', path)
        self.server = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.server
        spec.loader.exec_module(self.server)
        self.application = self.server.create_application()
        self.inner = self.application.app
        self.ready, self.stop = asyncio.Event(), asyncio.Event()
        async def lifespan():
            async with self.inner.router.lifespan_context(self.inner):
                self.ready.set()
                await self.stop.wait()
        self.server_task = asyncio.create_task(lifespan())
        await asyncio.wait_for(self.ready.wait(), timeout=3)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.application), base_url='http://127.0.0.1:8090',
                                       headers={'Accept':'application/json, text/event-stream', 'Authorization':'Bearer isolated-mcp-transport', 'MCP-Protocol-Version':'2025-11-25'})

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.server.client.aclose()
        self.stop.set()
        await self.server_task
        sys.modules.pop('isolated_mcp_server', None)

    async def rpc(self, method, params=None):
        return await self.client.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})

    async def test_authentication_and_only_four_tools(self):
        forbidden = await self.client.post('/mcp', headers={'Authorization':'Bearer invalid'}, json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
        self.assertEqual(401, forbidden.status_code)
        response = await self.rpc('tools/list')
        self.assertEqual(200, response.status_code, response.text)
        tools = response.json()['result']['tools']
        self.assertEqual({'search_matches','resolve_match','list_match_markets','record_expert_signals'}, {t['name'] for t in tools})
        for tool in tools:
            self.assertIn('attempt_id', tool['inputSchema']['required'])

    async def test_tool_call_reaches_business_api_and_preserves_context(self):
        captured = []
        def upstream(request):
            import json
            captured.append((request.url.path, json.loads(request.content), request.headers['authorization']))
            return httpx.Response(200, json={'items':[], 'next_cursor':None})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), headers={'Authorization':'Bearer isolated-mcp-transport'})
        response = await self.rpc('tools/call', {'name':'search_matches', 'arguments':{'review_id':'job','attempt_id':'attempt','team_query':'Alpha','phase':'all'}})
        self.assertEqual(200, response.status_code, response.text)
        self.assertFalse(response.json()['result'].get('isError', False))
        self.assertEqual('/internal/mcp/search_matches', captured[0][0])
        self.assertEqual('all', captured[0][1]['phase'])
        self.assertEqual('attempt', captured[0][1]['attempt_id'])
        self.assertEqual('Bearer isolated-mcp-transport', captured[0][2])

    async def test_transport_failure_is_sanitized_tool_error(self):
        def timeout(request):
            raise httpx.ReadTimeout('secret-upstream-url?token=must-not-leak', request=request)
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        response = await self.rpc('tools/call', {'name':'list_match_markets', 'arguments':{'review_id':'job','attempt_id':'attempt','fixture_id':'fixture'}})
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()['result']['isError'])
        self.assertNotIn('must-not-leak', response.text)
        self.assertIn('系统工具超时', response.text)

    async def test_record_schema_serializes_validated_picks(self):
        captured = []
        def upstream(request):
            import json
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={'accepted':[], 'rejected':[]})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        result = await self.rpc('tools/call', {'name':'record_expert_signals', 'arguments':{
            'review_id':'job','attempt_id':'attempt','signals':[{'fixture_id':'f','quote_id':'q','evidence_text':'大2.5'}]}})
        self.assertEqual(200, result.status_code, result.text)
        self.assertFalse(result.json()['result'].get('isError', False))
        self.assertEqual('大2.5', captured[0]['signals'][0]['evidence_text'])
        self.assertEqual(0.5, captured[0]['signals'][0]['confidence'])

    async def test_unknown_host_is_rejected_even_with_service_token(self):
        result = await self.client.post('/mcp', headers={'Host':'untrusted.invalid'}, json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
        self.assertEqual(421, result.status_code)
