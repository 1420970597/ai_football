import copy
import unittest
from unittest.mock import AsyncMock, patch
import test_mcp_transport as transport


class EmptySourceScopeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = transport.MCPTransportTests.asyncSetUp
    asyncTearDown = transport.MCPTransportTests.asyncTearDown

    def payload(self):
        return dict(review_id='job', attempt_id='attempt', fixture_id='leyu:5861112',
            market_kind='team_total', period='first_half', source_periods=['first_half'],
            market_query='大 1.5 / 2')

    def result(self):
        return dict(markets=[], market_filter=dict(market_kind='team_total',
            period='first_half', line='1.75', selection='over'),
            market_coverage=dict(total_quotes=66, matched_quotes=0, returned_quotes=0,
                complete_for_filter=True), refresh=dict(accepted=True, reason='written'))

    async def test_other_inventory_is_not_a_reason_to_change_explicit_period(self):
        payload, result = self.payload(), self.result()
        original = copy.deepcopy(result)
        native = AsyncMock(return_value=result)
        with patch.object(self.server, '_post', native):
            actual = await self.server._read_source_markets(payload)
        native.assert_awaited_once_with('list_match_markets', payload)
        self.assertEqual(original, result)
        self.assertEqual(original, {k:v for k,v in actual.items() if k != 'source_scope_unavailable'})
        guidance = actual['source_scope_unavailable']
        self.assertFalse(guidance['retry_same_scope'])
        self.assertFalse(guidance['relax_source_period'])
        self.assertEqual('first_half', guidance['scope']['period'])

    async def test_partial_unknown_period_and_positive_inventory_are_not_closed(self):
        cases = [({}, {'market_coverage': dict(self.result()['market_coverage'], complete_for_filter=False)}),
            ({'source_periods': None}, {}), ({'period': None}, {}),
            ({}, {'markets': [{'quote_id': 'real'}]}),
            ({}, {'market_coverage': dict(self.result()['market_coverage'], matched_quotes=1)}),
            ({}, {'market_filter': dict(market_kind='team_total', period='full_time')})]
        for inputs, response in cases:
            result = self.result() | response
            with patch.object(self.server, '_post', AsyncMock(return_value=result)):
                actual = await self.server._read_source_markets(self.payload() | inputs)
            self.assertIs(result, actual)

    async def test_native_error_is_preserved(self):
        with patch.object(self.server, '_post', AsyncMock(side_effect=self.server.ToolError('inactive'))):
            with self.assertRaisesRegex(self.server.ToolError, 'inactive'):
                await self.server._read_source_markets(self.payload())

    async def test_declared_unknown_period_does_not_read_or_forge_complete_inventory(self):
        for period in (None, 'first_half'):
            native = AsyncMock()
            with patch.object(self.server, '_post', native):
                result = await self.server._read_source_markets(
                    self.payload() | {'source_periods': [], 'period': period})
            native.assert_not_awaited()
            self.assertFalse(result['market_read_succeeded'])
            self.assertFalse(result['market_coverage']['complete_for_filter'])
            self.assertEqual('source_period_unknown',result['source_intent_recording']['reason'])
