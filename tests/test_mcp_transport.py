"""真实 MCP SDK 流式 HTTP 协议验证；使用隔离假业务响应。"""
import importlib.util
import asyncio
import json
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

    async def test_authentication_and_only_bounded_review_tools(self):
        forbidden = await self.client.post('/mcp', headers={'Authorization':'Bearer invalid'}, json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
        self.assertEqual(401, forbidden.status_code)
        response = await self.rpc('tools/list')
        self.assertEqual(200, response.status_code, response.text)
        tools = response.json()['result']['tools']
        self.assertEqual({'search_matches','resolve_match','list_match_markets','record_expert_signals','review_match_batch'}, {t['name'] for t in tools})
        for tool in tools:
            self.assertIn('attempt_id', tool['inputSchema']['required'])
        search = next(tool for tool in tools if tool['name'] == 'search_matches')
        self.assertEqual(100, search['inputSchema']['properties']['page_size']['default'])

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

    async def test_bounded_league_search_forwards_empty_team_without_inventing_resolution(self):
        captured = []

        async def upstream(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={'items': [{'id': 'candidate-only',
                'retrieval_basis': 'competition_time_candidate'}], 'next_cursor': None,
                'search_complete': True, 'resolution': {'resolved': False}})

        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        arguments = {'review_id': 'job', 'attempt_id': 'attempt', 'team_query': '',
            'competition_query': '捷克丙级联赛', 'sport': 'football',
            'from_utc': '2026-11-08T00:00:00Z', 'to_utc': '2026-11-09T00:00:00Z',
            'read_markets': True, 'market_kind': 'total', 'period': 'full_time'}
        response = await self.rpc('tools/call', {'name': 'search_matches', 'arguments': arguments})
        self.assertFalse(response.json()['result'].get('isError', False), response.text)
        self.assertEqual(1, len(captured), 'unresolved league candidates must not trigger a market read')
        for name in ('review_id', 'attempt_id', 'team_query', 'competition_query', 'sport', 'from_utc', 'to_utc'):
            self.assertEqual(arguments[name], captured[0][name])
        self.assertNotIn('market_result', response.json()['result'].get('structuredContent', {}))

    async def test_resolve_schema_explains_and_constrains_native_search_reference(self):
        tools = (await self.rpc('tools/list')).json()['result']['tools']
        tool = next(t for t in tools if t['name'] == 'resolve_match')
        reference = tool['inputSchema']['properties']['search_key']
        self.assertEqual(64, reference.get('minLength'))
        self.assertEqual(64, reference.get('maxLength'))
        self.assertEqual('^[0-9a-f]{64}$', reference.get('pattern'))
        self.assertIn('search_matches', reference.get('description', ''))
        self.assertIn('search_key', reference.get('description', ''))

    async def test_invalid_resolve_references_do_not_reach_business_api(self):
        captured = []
        def upstream(request):
            captured.append(request)
            return httpx.Response(200, json={'resolved': False})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        for reference in ('拉贾斯坦邦联 vs 戈库拉姆喀拉拉', 'a'*63, 'a'*65, 'z'*64, 'A'*64):
            with self.subTest(reference=reference):
                response = await self.rpc('tools/call', {'name': 'resolve_match', 'arguments': {
                    'review_id': 'job', 'attempt_id': 'attempt', 'search_key': reference,
                    'team_a': 'Alpha', 'team_b': 'Beta'}})
                self.assertTrue(response.json()['result'].get('isError', False))
        self.assertEqual([], captured)

    async def test_valid_resolve_reference_is_forwarded_verbatim(self):
        reference = '0123456789abcdef'*4
        captured = []
        def upstream(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={'resolved': False, 'reason': 'ambiguous'})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        response = await self.rpc('tools/call', {'name': 'resolve_match', 'arguments': {
            'review_id': 'job', 'attempt_id': 'attempt', 'search_key': reference,
            'team_a': 'Alpha', 'team_b': 'Beta'}})
        self.assertFalse(response.json()['result'].get('isError', False))
        self.assertEqual(1, len(captured))
        self.assertEqual(reference, captured[0]['search_key'])
        self.assertEqual(('job', 'attempt', 'Alpha', 'Beta'),
                         tuple(captured[0][k] for k in ('review_id', 'attempt_id', 'team_a', 'team_b')))

    async def test_search_schema_identifies_original_pair_and_opaque_cursor(self):
        tools = (await self.rpc('tools/list')).json()['result']['tools']
        properties = next(t for t in tools if t['name'] == 'search_matches')['inputSchema']['properties']
        for key in ('team_a', 'team_b'):
            self.assertIn('原文', properties[key].get('description', ''))
            self.assertIn('成对', properties[key].get('description', ''))
        self.assertIn('next_cursor', properties['cursor'].get('description', ''))

    async def test_ambiguous_period_guidance_preserves_native_mixed_results(self):
        native = {'expert': {'id': 'expert'}, 'accepted': [{'index': 0, 'signal_id': 'signal'}],
                  'rejected': [{'index': 1, 'reason': 'source_period_intent_ambiguous'},
                               {'index': 2, 'reason': 'quote_inactive_unknown_or_stale'}]}
        captured = []
        def upstream(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=native)
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        response = await self.rpc('tools/call', {'name': 'record_expert_signals', 'arguments': {
            'review_id': 'job', 'attempt_id': 'attempt',
            'signals': [{'fixture_id': 'fixture', 'quote_id': 'quote', 'evidence_text': '半大0.5'}]}})
        result = response.json()['result']
        self.assertFalse(result.get('isError', False))
        body = result.get('structuredContent') or json.loads(result['content'][0]['text'])
        self.assertEqual(native['accepted'], body['accepted'])
        self.assertEqual(native['rejected'], body['rejected'])
        self.assertEqual([{'index': 1, 'reason': 'source_period_intent_ambiguous',
                          'retry_same_evidence': False, 'action': 'report_unresolved'}], body.get('rejection_guidance'))
        self.assertEqual(1, len(captured))
        self.assertEqual(('job', 'attempt'), (captured[0]['review_id'], captured[0]['attempt_id']))

    async def test_recoverable_rejection_and_success_have_no_terminal_guidance(self):
        for native in ({'accepted': [{'index': 0}], 'rejected': []},
                       {'accepted': [], 'rejected': [{'index': 0, 'reason': 'quote_inactive_unknown_or_stale'}]},
                       {'accepted': [], 'rejected': [{'index': 0, 'reason': 'source_period_intent_conflict'}]}):
            with self.subTest(native=native):
                await self.server.client.aclose()
                self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=native)))
                result = (await self.rpc('tools/call', {'name': 'record_expert_signals', 'arguments': {
                    'review_id': 'job', 'attempt_id': 'attempt',
                    'signals': [{'fixture_id': 'fixture', 'quote_id': 'quote', 'evidence_text': '大0.5'}]}})).json()['result']
                self.assertFalse(result.get('isError', False))
                body = result.get('structuredContent') or json.loads(result['content'][0]['text'])
                self.assertEqual(native, body)

    async def test_search_optional_pair_schema_forwarding_and_resolution(self):
        tools = (await self.rpc('tools/list')).json()['result']['tools']
        self.assertEqual(5, len(tools))
        tool = next(t for t in tools if t['name'] == 'search_matches')
        for key in ('team_a', 'team_b'):
            self.assertIn(key, tool['inputSchema']['properties'])
            self.assertNotIn(key, tool['inputSchema']['required'])
        captured = []
        def upstream(request):
            import json
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={'search_complete': True, 'resolution': {
                'resolved': True, 'fixture': {'fixture_id': 'fixture-1', 'realtime_eligible': True}}})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        response = await self.rpc('tools/call', {'name': 'search_matches', 'arguments': {
            'review_id': 'job', 'attempt_id': 'attempt', 'team_query': 'Alpha',
            'team_a': 'Alpha', 'team_b': 'Beta'}})
        self.assertEqual(200, response.status_code)
        self.assertEqual(('Alpha', 'Beta'), (captured[0]['team_a'], captured[0]['team_b']))
        self.assertFalse(response.json()['result'].get('isError', False))

    async def test_market_intent_filters_are_optional_and_forwarded(self):
        tools_response = await self.rpc('tools/list')
        tool = next(item for item in tools_response.json()['result']['tools']
                    if item['name'] == 'list_match_markets')
        properties = tool['inputSchema']['properties']
        self.assertTrue({'market_kind', 'period', 'line', 'selection', 'market_query'} <= set(properties))
        self.assertFalse({'market_kind', 'period', 'line', 'selection', 'market_query'} &
                         set(tool['inputSchema'].get('required', [])))

        captured = []
        def upstream(request):
            import json
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={'fixture': {'id': 'fixture'}, 'markets': []})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        response = await self.rpc('tools/call', {'name': 'list_match_markets', 'arguments': {
            'review_id': 'job', 'attempt_id': 'attempt', 'fixture_id': 'fixture',
            'market_kind': 'total', 'period': 'full_time', 'line': '2.75',
            'selection': 'over', 'market_query': '全场大小'}})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({'review_id': 'job', 'attempt_id': 'attempt', 'fixture_id': 'fixture',
                          'market_kind': 'total', 'period': 'full_time', 'line': '2.75',
                          'selection': 'over', 'market_query': '全场大小'}, captured[0])

    async def test_search_and_resolve_can_read_only_their_bound_market(self):
        tools = (await self.rpc('tools/list')).json()['result']['tools']
        for name in ('search_matches', 'resolve_match'):
            with self.subTest(tool=name):
                schema = next(t['inputSchema'] for t in tools if t['name'] == name)
                self.assertFalse(schema['properties']['read_markets']['default'])
                calls = []
                resolution = {'resolved': True, 'fixture': {'id': 'bound-fixture', 'realtime_eligible': True}}
                def upstream(request):
                    path = request.url.path.rsplit('/', 1)[-1]
                    calls.append((path, json.loads(request.content)))
                    if path == 'list_match_markets':
                        return httpx.Response(200, json={'markets': [{'quote_id': 'native-quote', 'can_record': True}]})
                    return httpx.Response(200, json=resolution if name == 'resolve_match' else {
                        'search_complete': True, 'next_cursor': None, 'resolution': resolution})
                await self.server.client.aclose()
                self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
                arguments = {'review_id': 'job', 'attempt_id': 'current', 'team_a': 'Alpha', 'team_b': 'Beta',
                             'read_markets': True, 'market_kind': 'total', 'period': 'first_half'}
                arguments.update({'team_query': 'Alpha'} if name == 'search_matches' else {'search_key': 'a'*64})
                response = await self.rpc('tools/call', {'name': name, 'arguments': arguments})
                result = response.json()['result']
                self.assertFalse(result.get('isError', False), result)
                data = json.loads(result['content'][0]['text'])
                self.assertEqual('native-quote', data['market_result']['markets'][0]['quote_id'])
                self.assertEqual([name, 'list_match_markets'], [c[0] for c in calls])
                self.assertNotIn('read_markets', calls[0][1])
                self.assertNotIn('market_kind', calls[0][1])
                self.assertEqual({'review_id': 'job', 'attempt_id': 'current', 'fixture_id': 'bound-fixture',
                                 'market_kind': 'total', 'period': 'first_half'}, calls[1][1])

    async def test_composed_market_read_does_not_bypass_pagination_or_eligibility(self):
        for name in ('search_matches', 'resolve_match'):
            for resolution in ({'resolved': False, 'reason': 'ambiguous_fixture'},
                               {'resolved': False, 'reason': 'no_matching_fixture'},
                               {'resolved': True, 'fixture': {'id': 'finished', 'realtime_eligible': False}},
                               {'resolved': True, 'fixture': {'realtime_eligible': True}}):
                with self.subTest(tool=name, resolution=resolution):
                    calls = []
                    def upstream(request):
                        calls.append(request.url.path.rsplit('/', 1)[-1])
                        return httpx.Response(200, json=resolution if name == 'resolve_match' else {
                            'search_complete': True, 'next_cursor': None, 'resolution': resolution})
                    await self.server.client.aclose()
                    self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
                    arguments = {'review_id': 'job', 'attempt_id': 'attempt', 'read_markets': True,
                                 'team_a': 'Alpha', 'team_b': 'Beta'}
                    arguments.update({'team_query': 'Alpha'} if name == 'search_matches' else {'search_key': 'a'*64})
                    result = (await self.rpc('tools/call', {'name': name, 'arguments': arguments})).json()['result']
                    self.assertFalse(result.get('isError', False))
                    self.assertEqual([name], calls)

    async def test_composed_read_waits_for_last_page_and_preserves_lease_failure(self):
        calls = []
        def upstream(request):
            path = request.url.path.rsplit('/', 1)[-1]
            body = json.loads(request.content); calls.append((path, body))
            if path == 'list_match_markets':
                return httpx.Response(409, json={'detail': 'review attempt is no longer active'})
            complete = body['cursor'] == 'next-page'
            return httpx.Response(200, json={'search_complete': complete,
                'next_cursor': None if complete else 'next-page', 'resolution': {
                    'resolved': True, 'fixture': {'id': 'fixture', 'realtime_eligible': True}}})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        arguments = {'review_id': 'job', 'attempt_id': 'attempt', 'team_query': 'Alpha',
                     'team_a': 'Alpha', 'team_b': 'Beta', 'read_markets': True}
        first = (await self.rpc('tools/call', {'name': 'search_matches', 'arguments': arguments})).json()['result']
        self.assertFalse(first.get('isError', False))
        self.assertEqual(['search_matches'], [c[0] for c in calls])
        arguments['cursor'] = 'next-page'
        second = (await self.rpc('tools/call', {'name': 'search_matches', 'arguments': arguments})).json()['result']
        self.assertTrue(second['isError'])
        self.assertEqual(['search_matches', 'search_matches', 'list_match_markets'], [c[0] for c in calls])
        self.assertEqual('next-page', calls[1][1]['cursor'])
        self.assertEqual('attempt', calls[2][1]['attempt_id'])
        self.assertEqual(0, self.server.pending_market_requests)
        self.assertFalse(self.server.market_gate.locked())

    async def test_composed_read_failure_keeps_bounded_receipt_as_protocol_error(self):
        for name in ('search_matches', 'resolve_match'):
            for failure in ('lease', 'timeout'):
                with self.subTest(tool=name, failure=failure):
                    calls = []
                    resolution = {'resolved': True, 'fixture': {
                        'id': 'bound-fixture', 'realtime_eligible': True,
                        'home_team': 'Alpha', 'away_team': 'Beta', 'quote_id': 'not-a-market-read'}}
                    def upstream(request):
                        path = request.url.path.rsplit('/', 1)[-1]
                        calls.append((path, json.loads(request.content)))
                        if path == 'list_match_markets':
                            if failure == 'timeout':
                                raise httpx.ReadTimeout('isolated protocol timeout', request=request)
                            return httpx.Response(409, json={'detail': 'review attempt is no longer active'})
                        if name == 'resolve_match':
                            return httpx.Response(200, json=resolution)
                        return httpx.Response(200, json={'search_key': 'a'*64,
                            'search_complete': True, 'next_cursor': None, 'resolution': resolution,
                            'items': [{'quote_id': 'must-not-leak'}]})
                    await self.server.client.aclose()
                    self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
                    arguments = {'review_id': 'job', 'attempt_id': 'current', 'team_a': 'Alpha',
                        'team_b': 'Beta', 'read_markets': True, 'market_kind': 'total', 'period': 'first_half'}
                    arguments.update({'team_query': 'Alpha'} if name == 'search_matches' else {'search_key': 'a'*64})
                    result = (await self.rpc('tools/call', {'name': name, 'arguments': arguments})).json()['result']
                    self.assertTrue(result['isError'])
                    text = result['content'][0]['text']
                    error = json.loads(text[text.index('{'):])
                    self.assertFalse(error['market_read_succeeded'])
                    self.assertEqual('list_match_markets', error['failed_operation'])
                    self.assertNotIn('market_result', error)
                    receipt = error['completed_receipt']
                    self.assertEqual(('job', 'current', 'a'*64),
                        (receipt['review_id'], receipt['attempt_id'], receipt['search_key']))
                    self.assertEqual('bound-fixture', receipt['resolution']['fixture']['id'])
                    self.assertNotIn('quote_id', receipt['resolution']['fixture'])
                    self.assertNotIn('items', receipt)
                    self.assertIn('超时' if failure == 'timeout' else 'no longer active', error['error'])
                    self.assertEqual([name, 'list_match_markets'], [c[0] for c in calls])
                    self.assertEqual('current', calls[1][1]['attempt_id'])
                    self.assertEqual(0, self.server.pending_market_requests)
                    self.assertEqual(0, self.server.pending_market_calls)
                    self.assertFalse(self.server.market_gate.locked())
                    self.assertEqual({}, self.server.market_reads)

    async def test_initial_search_failure_never_invents_a_completed_receipt(self):
        calls = []
        def upstream(request):
            calls.append(request.url.path.rsplit('/', 1)[-1])
            return httpx.Response(409, json={'detail': 'review attempt is no longer active'})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        result = (await self.rpc('tools/call', {'name': 'search_matches', 'arguments': {
            'review_id': 'job', 'attempt_id': 'current', 'team_query': 'Alpha',
            'team_a': 'Alpha', 'team_b': 'Beta', 'read_markets': True}})).json()['result']
        self.assertTrue(result['isError'])
        self.assertNotIn('completed_receipt', result['content'][0]['text'])
        self.assertEqual(['search_matches'], calls)

    async def test_market_filters_alone_do_not_enable_composed_reads(self):
        for name in ('search_matches', 'resolve_match'):
            calls = []
            def upstream(request):
                calls.append(request.url.path.rsplit('/', 1)[-1])
                resolution = {'resolved': True, 'fixture': {'id': 'fixture', 'realtime_eligible': True}}
                return httpx.Response(200, json=resolution if name == 'resolve_match' else {
                    'search_complete': True, 'next_cursor': None, 'resolution': resolution})
            await self.server.client.aclose()
            self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
            arguments = {'review_id': 'job', 'attempt_id': 'attempt', 'team_a': 'Alpha', 'team_b': 'Beta',
                         'market_kind': 'total', 'period': 'first_half'}
            arguments.update({'team_query': 'Alpha'} if name == 'search_matches' else {'search_key': 'a'*64})
            result = (await self.rpc('tools/call', {'name': name, 'arguments': arguments})).json()['result']
            self.assertFalse(result.get('isError', False))
            self.assertEqual([name], calls)

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

    async def test_market_operations_share_admission_while_search_remains_parallel(self):
        entered, release = asyncio.Event(), asyncio.Event()
        paths, active, peak = [], 0, 0
        async def upstream(request):
            nonlocal active, peak
            path = request.url.path.rsplit('/', 1)[-1]
            paths.append(path)
            if path in {'list_match_markets', 'record_expert_signals'}:
                active += 1
                peak = max(peak, active)
                try:
                    if path == 'list_match_markets':
                        entered.set()
                        await release.wait()
                finally:
                    active -= 1
            return httpx.Response(200, json={'ok': True})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        first = asyncio.create_task(self.rpc('tools/call', {'name': 'list_match_markets', 'arguments': {
            'review_id': 'job', 'attempt_id': 'attempt', 'fixture_id': 'fixture'}}))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(self.rpc('tools/call', {'name': 'record_expert_signals', 'arguments': {
            'review_id': 'job', 'attempt_id': 'attempt', 'signals': [
                {'fixture_id': 'fixture', 'quote_id': 'quote', 'evidence_text': '全场大2.5'}]}}))
        try:
            for path in ('search_matches', 'resolve_match'):
                self.assertEqual({'ok': True}, await asyncio.wait_for(self.server._post(path, {}), 1))
            self.assertNotIn('record_expert_signals', paths)
        finally:
            release.set()
            results = await asyncio.gather(first, second)
        for result in results:
            self.assertEqual(200, result.status_code)
            self.assertFalse(result.json()['result'].get('isError', False))
        self.assertEqual(1, peak)
        self.assertIn('record_expert_signals', paths)

    async def test_cancelled_market_request_releases_admission(self):
        entered = asyncio.Event()
        async def upstream(request):
            if request.url.path.endswith('/list_match_markets'):
                entered.set()
                await asyncio.Event().wait()
            return httpx.Response(200, json={'accepted': []})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        first = asyncio.create_task(self.server._post('list_match_markets', {}))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(self.server._post('record_expert_signals', {}))
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual({'accepted': []}, await asyncio.wait_for(second, 1))
        self.assertEqual(0, self.server.pending_market_calls)

    async def test_market_wait_timeout_and_capacity_do_not_send_requests(self):
        entered, release = asyncio.Event(), asyncio.Event()
        paths = []
        async def upstream(request):
            paths.append(request.url.path)
            entered.set()
            await release.wait()
            return httpx.Response(200, json={'ok': True})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.server.MARKET_WAIT_SECONDS = .03
        self.server.MAX_PENDING_MARKET_CALLS = 2
        first = asyncio.create_task(self.server._post('list_match_markets', {}))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(self.server._post('record_expert_signals', {}))
        await asyncio.sleep(0)
        try:
            with self.assertRaisesRegex(self.server.ToolError, '队列繁忙'):
                await self.server._post('list_match_markets', {})
            with self.assertRaisesRegex(self.server.ToolError, '等待超时'):
                await second
            self.assertEqual(1, len(paths))
        finally:
            release.set()
            await first
        self.assertEqual({'ok': True}, await self.server._post('record_expert_signals', {}))
        self.assertEqual(0, self.server.pending_market_calls)

    async def test_failed_market_http_releases_admission(self):
        statuses = iter([503, 200])
        def upstream(request):
            return httpx.Response(next(statuses), json={'detail': '暂不可用'})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        with self.assertRaises(self.server.ToolError):
            await self.server._post('list_match_markets', {})
        self.assertEqual({'detail': '暂不可用'}, await self.server._post('record_expert_signals', {}))
        self.assertEqual(0, self.server.pending_market_calls)

    async def test_identical_market_reads_share_only_inflight_operation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def upstream(request):
            calls.append(request.content)
            entered.set()
            await release.wait()
            return httpx.Response(200, json={'markets': [{'quote_id': 'quote'}]})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        payload = {'review_id': 'job', 'attempt_id': 'attempt', 'fixture_id': 'fixture', 'line': None}
        first = asyncio.create_task(self.server._post('list_match_markets', payload))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(self.server._post('list_match_markets', dict(reversed(list(payload.items())))))
        await asyncio.sleep(0)
        self.assertEqual(1, len(calls))
        self.assertEqual(2, self.server.pending_market_requests)
        release.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(results[0], results[1])
        self.assertFalse(self.server.market_reads)
        await self.server._post('list_match_markets', payload)
        self.assertEqual(2, len(calls))
        self.assertEqual(0, self.server.pending_market_requests)

    async def test_market_read_identity_isolates_attempt_and_every_filter(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def upstream(request):
            calls.append(request.content)
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return httpx.Response(200, json={'ok': True})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        payload = {'review_id': 'job', 'attempt_id': 'attempt', 'fixture_id': 'fixture',
                   'market_kind': 'handicap', 'period': 'full_time', 'line': '-6.5',
                   'selection': 'away', 'market_query': '让分'}
        variants = [payload | {key: 'other'} for key in payload]
        # Keep each batch within the original eight-request admission budget.
        for offset in range(0, len(variants), 7):
            calls.clear(); entered.clear(); release.clear()
            first = asyncio.create_task(self.server._post('list_match_markets', payload))
            await asyncio.wait_for(entered.wait(), 1)
            others = [asyncio.create_task(self.server._post('list_match_markets', item))
                      for item in variants[offset:offset+7]]
            await asyncio.sleep(0)
            self.assertEqual(1+len(others), len(self.server.market_reads))
            release.set()
            await asyncio.gather(first, *others)
            self.assertEqual(1+len(others), len(calls))
            self.assertFalse(self.server.market_reads)

    async def test_shared_read_one_cancel_keeps_other_waiter_and_final_cancel_drains(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls, cancelled = [], []
        async def upstream(request):
            calls.append(request.content)
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return httpx.Response(200, json={'ok': True})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        payload = {'review_id': 'job', 'attempt_id': 'attempt', 'fixture_id': 'fixture'}
        first = asyncio.create_task(self.server._post('list_match_markets', payload))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(self.server._post('list_match_markets', payload))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(cancelled)
        self.assertEqual(1, self.server.pending_market_calls)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        self.assertEqual([True], cancelled)
        self.assertFalse(self.server.market_reads)
        self.assertEqual(0, self.server.pending_market_calls)
        self.assertEqual(0, self.server.pending_market_requests)
        release.set()
        self.assertEqual({'ok': True}, await self.server._post('list_match_markets', payload))
        self.assertEqual(2, len(calls))

    async def test_shared_read_failure_is_not_cached_and_writes_never_coalesce(self):
        entered, release = asyncio.Event(), asyncio.Event()
        paths = []
        async def upstream(request):
            paths.append(request.url.path)
            if request.url.path.endswith('/list_match_markets'):
                entered.set()
                await release.wait()
                return httpx.Response(503, json={'detail': '暂不可用'})
            return httpx.Response(200, json={'accepted': []})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        payload = {'review_id': 'job', 'attempt_id': 'attempt', 'fixture_id': 'fixture'}
        first = asyncio.create_task(self.server._post('list_match_markets', payload))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(self.server._post('list_match_markets', payload))
        await asyncio.sleep(0)
        release.set()
        errors = await asyncio.gather(first, second, return_exceptions=True)
        self.assertTrue(all(isinstance(error, self.server.ToolError) for error in errors))
        self.assertEqual(1, len(paths))
        self.assertFalse(self.server.market_reads)
        with self.assertRaises(self.server.ToolError):
            await self.server._post('list_match_markets', payload)
        writes = await asyncio.gather(*(self.server._post('record_expert_signals', payload) for _ in range(2)))
        self.assertEqual([{'accepted': []}, {'accepted': []}], writes)
        self.assertEqual(4, len(paths))
        self.assertEqual(0, self.server.pending_market_requests)

    async def test_shared_mcp_read_survives_one_waiter_cancellation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def upstream(request):
            calls.append(request.content)
            entered.set()
            await release.wait()
            return httpx.Response(200, json={'markets': [], 'fixture': {'id': 'fixture'}})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        arguments = {'review_id': 'job', 'attempt_id': 'attempt', 'fixture_id': 'fixture'}
        first = asyncio.create_task(self.rpc('tools/call', {
            'name': 'list_match_markets', 'arguments': arguments}))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(self.rpc('tools/call', {
            'name': 'list_match_markets', 'arguments': arguments}))
        for _ in range(100):
            if self.server.pending_market_requests == 2:
                break
            await asyncio.sleep(.001)
        self.assertEqual(2, self.server.pending_market_requests)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        result = await asyncio.wait_for(second, 1)
        self.assertEqual(200, result.status_code)
        self.assertFalse(result.json()['result'].get('isError', False))
        self.assertEqual(1, len(calls))
        self.assertFalse(self.server.market_reads)
        self.assertEqual(0, self.server.pending_market_calls)
        self.assertEqual(0, self.server.pending_market_requests)
