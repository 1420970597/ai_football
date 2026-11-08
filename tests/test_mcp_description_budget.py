"""真实MCP HTTP列表的重复说明预算；不调用模型或真实业务服务。"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx
from mcp.server import MCPServer
import yaml


ROOT = Path(__file__).resolve().parents[1]
PROFILE = {'X-Football-Tool-Profile': 'parallel-compact-v1'}


class MCPDescriptionBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        os.environ['EXPERT_MCP_TOKEN'] = 'isolated-description-budget'
        spec = importlib.util.spec_from_file_location(
            'isolated_description_server', ROOT / 'services/hermes-mcp/server.py')
        self.server = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.server
        spec.loader.exec_module(self.server)
        self.app = self.server.create_application()
        self.ready, self.stop = asyncio.Event(), asyncio.Event()

        async def lifespan():
            async with self.app.app.router.lifespan_context(self.app.app):
                self.ready.set()
                await self.stop.wait()

        self.server_task = asyncio.create_task(lifespan())
        await asyncio.wait_for(self.ready.wait(), 3)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url='http://127.0.0.1:8090',
            headers={'Authorization': 'Bearer isolated-description-budget',
                     'Accept': 'application/json, text/event-stream',
                     'MCP-Protocol-Version': '2025-11-25'})

    async def asyncTearDown(self):
        await self.client.aclose()
        self.stop.set()
        await self.server_task
        await self.server.client.aclose()
        sys.modules.pop('isolated_description_server', None)

    async def rpc(self, method='tools/list', params=None, headers=None):
        return await self.client.post('/mcp', headers=headers or {}, json={
            'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})

    async def tools(self, headers=None):
        response = await self.rpc(headers=headers)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()['result']['tools']

    async def native_tools(self):
        return [tool.model_dump(by_alias=True, exclude_none=True)
                for tool in await MCPServer.list_tools(self.server.mcp)]

    def assert_primary_unchanged(self, expected, actual):
        self.assertEqual(expected, actual)

    def assert_only_top_descriptions_changed(self, primary, parallel):
        self.assertEqual(4, len(primary))
        self.assertEqual([t['name'] for t in primary], [t['name'] for t in parallel])
        for original, compact in zip(primary, parallel):
            self.assertEqual({k: v for k, v in original.items() if k != 'description'},
                             {k: v for k, v in compact.items() if k != 'description'})
            self.assertIn('football', compact.get('description', ''))
        before = len(json.dumps(primary, ensure_ascii=False).encode())
        after = len(json.dumps(parallel, ensure_ascii=False).encode())
        self.assertGreater(before - after, 1500)

    async def test_default_list_is_native_including_every_description(self):
        self.assert_primary_unchanged(await self.native_tools(), await self.tools())

    async def test_parallel_list_only_shortens_top_level_descriptions(self):
        primary = await self.tools()
        self.assert_only_top_descriptions_changed(primary, await self.tools(PROFILE))
        self.assert_primary_unchanged(primary, await self.tools())

    async def test_unknown_or_duplicate_profiles_do_not_change_native_list(self):
        primary = await self.tools()
        for value in ('', 'compact', 'parallel-compact-v2', 'parallel-compact-v1,full'):
            with self.subTest(value=value):
                self.assert_primary_unchanged(primary, await self.tools({
                    'X-Football-Tool-Profile': value}))
        duplicate = [('X-Football-Tool-Profile', 'parallel-compact-v1'),
                     ('X-Football-Tool-Profile', 'full')]
        self.assert_primary_unchanged(primary, await self.tools(duplicate))
        self.assert_primary_unchanged(primary, await self.tools(list(reversed(duplicate))))

    async def test_simultaneous_lists_keep_independent_request_profiles(self):
        primary = await self.native_tools()
        original = MCPServer.list_tools
        entered = 0
        together = asyncio.Event()

        async def delayed(instance):
            nonlocal entered
            entered += 1
            if entered == 2:
                together.set()
            await asyncio.wait_for(together.wait(), 3)
            await asyncio.sleep(.01)
            return await original(instance)

        with patch.object(MCPServer, 'list_tools', delayed):
            full, compact = await asyncio.gather(self.tools(), self.tools(PROFILE))
        self.assert_primary_unchanged(primary, full)
        self.assert_only_top_descriptions_changed(full, compact)
        self.assert_primary_unchanged(primary, await self.tools())

    async def test_exception_and_cancellation_reset_profile_in_caller_context(self):
        primary = await self.native_tools()
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                async def failing(scope, receive, send):
                    self.assert_only_top_descriptions_changed(
                        primary, [t.model_dump(by_alias=True, exclude_none=True)
                                  for t in await self.server.mcp.list_tools()])
                    if cancelled:
                        raise asyncio.CancelledError()
                    raise RuntimeError('isolated transport failure')

                wrapper = self.server.BearerAuthentication(failing)
                scope = {'type': 'http', 'headers': [
                    (b'authorization', b'Bearer isolated-description-budget'),
                    (b'x-football-tool-profile', b'parallel-compact-v1')]}
                with self.assertRaises(asyncio.CancelledError if cancelled else RuntimeError):
                    await wrapper(scope, None, None)
                self.assert_primary_unchanged(primary, [
                    t.model_dump(by_alias=True, exclude_none=True)
                    for t in await self.server.mcp.list_tools()])

    async def test_profile_never_grants_authentication_or_extra_tools(self):
        response = await self.rpc(headers={**PROFILE, 'Authorization': 'Bearer invalid'})
        self.assertEqual(401, response.status_code)
        response = await self.rpc('tools/call', {'name': 'place_bet', 'arguments': {}}, PROFILE)
        self.assertTrue(response.json()['result']['isError'])
        self.assertEqual({'search_matches', 'resolve_match', 'list_match_markets',
                          'record_expert_signals'}, {t['name'] for t in await self.tools(PROFILE)})

    async def test_business_parameters_and_invalid_reference_stay_identical(self):
        captured = []

        def upstream(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={'resolved': False, 'reason': 'ambiguous'})

        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        arguments = {'review_id': 'job', 'attempt_id': 'attempt',
                     'search_key': '0123456789abcdef' * 4, 'team_a': 'Alpha', 'team_b': 'Beta'}
        outputs = []
        for headers in ({}, PROFILE):
            result = await self.rpc('tools/call', {'name': 'resolve_match',
                'arguments': arguments}, headers)
            outputs.append(result.json()['result'])
            invalid = await self.rpc('tools/call', {'name': 'resolve_match',
                'arguments': {**arguments, 'search_key': 'invented-team-id'}}, headers)
            self.assertTrue(invalid.json()['result']['isError'])
        self.assertEqual(2, len(captured))
        self.assertEqual(captured[0], captured[1])
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(arguments['search_key'], captured[0]['search_key'])

    def test_only_secondary_connection_declares_fixed_non_secret_profile(self):
        config = yaml.safe_load((ROOT / 'integrations/hermes/config.yaml').read_text())
        servers = config['mcp_servers']
        self.assertEqual({'football', 'football_parallel'}, set(servers))
        primary, parallel = servers['football'], servers['football_parallel']
        self.assertEqual('parallel-compact-v1',
                         parallel['headers'].get('X-Football-Tool-Profile'))
        self.assertNotIn('X-Football-Tool-Profile', primary['headers'])
        without_profile = {**parallel, 'headers': {k: v for k, v in parallel['headers'].items()
                                                 if k != 'X-Football-Tool-Profile'}}
        self.assertEqual(primary, without_profile)
