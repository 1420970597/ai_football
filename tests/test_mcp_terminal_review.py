import asyncio
import json
import unittest

import httpx
import test_mcp_transport as support


class TerminalReviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.MCPTransportTests.asyncSetUp
    asyncTearDown = support.MCPTransportTests.asyncTearDown
    rpc = support.MCPTransportTests.rpc

    async def mock(self, handler):
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def invoke(self, attempt='old'):
        response = await self.rpc('tools/call', {'name': 'list_match_markets', 'arguments': {
            'review_id': 'job', 'attempt_id': attempt, 'fixture_id': 'fixture'}})
        return response.json()['result']

    async def test_inactive_attempt_is_terminal_without_transport_failure(self):
        calls = []
        def upstream(request):
            calls.append(request.url.path)
            return httpx.Response(409, json={'detail': 'review attempt is no longer active'})
        await self.mock(upstream)
        for _ in range(4):
            result = await self.invoke()
            self.assertFalse(result.get('isError', False))
            body = json.loads(result['content'][0]['text'])
            self.assertTrue(body['stop_review'])
            self.assertFalse(body['retryable'])
            self.assertEqual([], body['accepted'])
            self.assertEqual('attempt_inactive', body['review_terminal_state']['reason'])
        self.assertEqual(4, len(calls))

    async def test_concurrent_new_attempt_is_not_marked_terminal(self):
        async def upstream(request):
            if json.loads(request.content)['attempt_id'] == 'old':
                await asyncio.sleep(.01)
                return httpx.Response(409, json={'detail': 'review attempt is no longer active'})
            return httpx.Response(200, json={'markets': [], 'fresh': False})
        await self.mock(upstream)
        old, new = await asyncio.gather(self.invoke(), self.invoke('new'))
        self.assertTrue(json.loads(old['content'][0]['text'])['stop_review'])
        self.assertEqual({'markets': [], 'fresh': False}, json.loads(new['content'][0]['text']))

    async def test_unknown_conflict_auth_and_network_errors_stay_errors(self):
        for status, detail in ((409, 'unknown conflict'), (401, 'not authorized'), (503, 'unavailable')):
            await self.mock(lambda request: httpx.Response(status, json={'detail': detail}))
            result = await self.invoke()
            self.assertTrue(result['isError'])
            self.assertIn(detail, result['content'][0]['text'])

    async def test_deadline_is_not_reported_as_accepted(self):
        await self.mock(lambda request: httpx.Response(409, json={'detail': {
            'category': 'detection_deadline_exceeded', 'limit_seconds': 20}}))
        result = await self.invoke()
        body = json.loads(result['content'][0]['text'])
        self.assertFalse(result.get('isError', False))
        self.assertEqual('detection_deadline_exceeded', body['review_terminal_state']['reason'])
        self.assertEqual([], body['accepted'])
