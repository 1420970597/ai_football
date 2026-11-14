import unittest
from unittest.mock import AsyncMock, patch
import test_mcp_transport as support


class RefreshSpacingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.MCPTransportTests.asyncSetUp
    asyncTearDown = support.MCPTransportTests.asyncTearDown

    async def test_known_spacing_waits_once_preserving_exact_attempt_and_filters(self):
        pending = {'refresh': {'reason': 'refresh_throttled', 'accepted': False, 'retry_after_seconds': 2}}
        current = {'markets': [], 'refresh': {'reason': 'written', 'accepted': True}}
        payload = {'review_id': 'job', 'attempt_id': 'current', 'fixture_id': 'fixture',
            'period': 'full_time', 'market_kind': 'handicap', 'source_periods': ['full_time']}
        with patch.object(self.server, '_market_request', AsyncMock(side_effect=[pending, current])) as request, \
             patch.object(self.server.asyncio, 'sleep', AsyncMock()) as sleep:
            self.assertEqual(current, await self.server._post('list_match_markets', payload))
        sleep.assert_awaited_once_with(2)
        self.assertEqual(2, request.await_count)
        for call in request.await_args_list:
            self.assertEqual(('list_match_markets', payload), call.args)
        self.assertEqual(0, self.server.pending_market_requests)

    async def test_repeated_refusal_does_not_loop(self):
        pending = {'refresh': {'reason': 'refresh_throttled', 'accepted': False, 'retry_after_seconds': 1}}
        with patch.object(self.server, '_market_request', AsyncMock(return_value=pending)) as request, \
             patch.object(self.server.asyncio, 'sleep', AsyncMock()):
            self.assertEqual(pending, await self.server._post('list_match_markets', {}))
        self.assertEqual(2, request.await_count)

    async def test_writes_cooldown_unknown_and_invalid_wait_never_retry(self):
        for path, reason, wait in [('record_expert_signals', 'refresh_throttled', 1),
                ('list_match_markets', 'upstream_cooldown', 1),
                ('list_match_markets', 'refresh_failed', 1),
                ('list_match_markets', 'refresh_throttled', 3),
                ('list_match_markets', 'refresh_throttled', True)]:
            pending = {'refresh': {'reason': reason, 'accepted': False, 'retry_after_seconds': wait}}
            with patch.object(self.server, '_market_request', AsyncMock(return_value=pending)) as request, \
                 patch.object(self.server.asyncio, 'sleep', AsyncMock()) as sleep:
                self.assertEqual(pending, await self.server._post(path, {}))
            self.assertEqual(1, request.await_count)
            sleep.assert_not_awaited()
