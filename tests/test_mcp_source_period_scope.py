"""Source declarations cross the existing MCP boundary without new authority."""
import json
import unittest

import test_mcp_composed_query as composed


class MCPSourcePeriodScopeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=composed.ComposedMarketQueryTests.asyncSetUp
    asyncTearDown=composed.ComposedMarketQueryTests.asyncTearDown
    rpc=composed.ComposedMarketQueryTests.rpc
    install=composed.ComposedMarketQueryTests.install
    call=composed.ComposedMarketQueryTests.call

    async def test_both_composed_calls_forward_the_complete_source_scope_only_to_markets(self):
        for name in ('search_matches','resolve_match'):
            with self.subTest(name=name):
                calls=await self.install()
                result=await self.call(name,source_periods=['full_time','first_half'])
                self.assertFalse(result.get('isError',False))
                self.assertNotIn('source_periods',calls[0][1])
                self.assertEqual(['full_time','first_half'],calls[1][1]['source_periods'])

    async def test_new_profile_unknown_is_explicit_and_legacy_omission_stays_compatible(self):
        calls=await self.install()
        args={'review_id':'job','attempt_id':'attempt','fixture_id':'fixture','period':'first_half'}
        result=await self.rpc('tools/call',{'name':'list_match_markets','arguments':args})
        self.assertFalse(result.json()['result'].get('isError',False))
        self.assertNotIn('source_periods',calls[-1][1])
        self.client.headers['X-Football-Tool-Profile']='market-intent-v1'
        result=await self.rpc('tools/call',{'name':'list_match_markets','arguments':args})
        self.assertFalse(result.json()['result'].get('isError',False))
        self.assertEqual([],calls[-1][1]['source_periods'])

    async def test_new_profile_requires_source_scope_on_all_market_capable_tools(self):
        self.client.headers['X-Football-Tool-Profile']='parallel-intent-v1'
        tools=(await self.rpc('tools/list')).json()['result']['tools']
        self.assertEqual(5,len(tools))
        for tool in tools:
            if tool['name'] in {'record_expert_signals','review_match_batch'}:continue
            self.assertIn('source_periods',tool['inputSchema']['required'])
            self.assertNotIn('default',tool['inputSchema']['properties']['source_periods'])

    async def test_invalid_scope_fails_before_any_business_request(self):
        for scope in (['quarter'],['first_half']*4,'full_time'):
            with self.subTest(scope=scope):
                calls=await self.install()
                result=await self.call(source_periods=scope)
                self.assertTrue(result.get('isError'),result)
                self.assertEqual([],calls)

    async def test_explicit_unknown_still_does_not_record_or_change_search_scope(self):
        calls=await self.install()
        result=await self.call(read_markets=False,source_periods=[])
        self.assertFalse(result.get('isError',False))
        self.assertEqual(1,len(calls));self.assertNotIn('source_periods',calls[0][1])
