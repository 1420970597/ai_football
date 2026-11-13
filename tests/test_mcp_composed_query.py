"""Preserve explicit source totals filters in the existing composed MCP read."""
import json
from pathlib import Path
import unittest

import httpx
import test_mcp_transport as base


class ComposedMarketQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await base.MCPTransportTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await base.MCPTransportTests.asyncTearDown(self)

    rpc = base.MCPTransportTests.rpc

    async def install(self, *, resolved=True, complete=True, market_status=200):
        calls=[]
        fixture={'id':'leyu:verified','realtime_eligible':True,'phase':'scheduled'}
        def upstream(request):
            body=json.loads(request.content);calls.append((request.url.path,body))
            if request.url.path.endswith('/search_matches'):
                return httpx.Response(200,json={'search_complete':complete,'next_cursor':None if complete else 'native-next','resolution':{'resolved':resolved,'fixture':fixture}})
            if request.url.path.endswith('/resolve_match'):
                return httpx.Response(200,json={'resolved':resolved,'fixture':fixture})
            return httpx.Response(market_status,json={'markets':[],'detail':'attempt lease expired'}if market_status!=200 else {'markets':[],'market_coverage':{'total_quotes':0,'complete_for_filter':True}})
        await self.server.client.aclose()
        self.server.client=httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        return calls

    async def call(self,name='search_matches',**updates):
        arguments={'review_id':'job','attempt_id':'attempt','read_markets':True,'market_kind':'total','period':'full_time','market_query':'小3.5'}
        arguments.update({'team_query':'source team'}if name=='search_matches'else {'search_key':'a'*64,'team_a':'source home','team_b':'source away'})
        arguments.update(updates)
        response=await self.rpc('tools/call',{'name':name,'arguments':arguments})
        self.assertEqual(200,response.status_code)
        return response.json()['result']

    async def test_both_existing_tools_advertise_bounded_optional_query(self):
        tools=(await self.rpc('tools/list')).json()['result']['tools']
        self.assertEqual({'search_matches','resolve_match','list_match_markets','record_expert_signals','review_match_batch'},{t['name']for t in tools})
        for name in ('search_matches','resolve_match'):
            schema=next(t for t in tools if t['name']==name)['inputSchema']
            query=schema['properties']['market_query']
            self.assertNotIn('market_query',schema.get('required',[]))
            self.assertTrue(any(x.get('maxLength')==120 for x in query.get('anyOf',[query])))

    async def test_search_carries_exact_source_query_only_into_market_read(self):
        calls=await self.install();result=await self.call()
        self.assertFalse(result.get('isError',False),result)
        self.assertEqual(2,len(calls));self.assertNotIn('market_query',calls[0][1])
        self.assertEqual('小3.5',calls[1][1]['market_query'])
        self.assertEqual({'job'},{body['review_id']for _,body in calls})
        self.assertEqual({'attempt'},{body['attempt_id']for _,body in calls})
        self.assertEqual('leyu:verified',calls[1][1]['fixture_id'])
        self.assertEqual('full_time',calls[1][1]['period'])

    async def test_resolve_preserves_split_total_query_and_period(self):
        calls=await self.install();result=await self.call('resolve_match',market_query='大0.5/1',period='first_half')
        self.assertFalse(result.get('isError',False),result)
        self.assertEqual(2,len(calls));self.assertNotIn('market_query',calls[0][1])
        self.assertEqual('大0.5/1',calls[1][1]['market_query']);self.assertEqual('first_half',calls[1][1]['period'])
        self.assertNotIn('line',calls[1][1],'MCP must not guess or normalize provider lines')

    async def test_query_does_not_bypass_incomplete_or_ambiguous_search(self):
        for complete,resolved in ((False,True),(True,False)):
            with self.subTest(complete=complete,resolved=resolved):
                calls=await self.install(complete=complete,resolved=resolved);result=await self.call()
                self.assertFalse(result.get('isError',False),result);self.assertEqual(1,len(calls))

    async def test_failed_market_read_keeps_completed_binding_without_recording(self):
        calls=await self.install(market_status=409);result=await self.call('resolve_match')
        self.assertTrue(result.get('isError'),result)
        content=' '.join(c.get('text','')for c in result.get('content',[]))
        self.assertIn('completed_receipt',content);self.assertIn('market_read_succeeded',content)
        self.assertEqual(2,len(calls));self.assertFalse(any(path.endswith('/record_expert_signals')for path,_ in calls))

    async def test_overlong_query_rejected_before_business_request(self):
        calls=await self.install();result=await self.call(market_query='大'*121)
        self.assertTrue(result.get('isError'),result);self.assertEqual([],calls)

    async def test_skill_reuses_exact_total_query_and_complete_empty_inventory(self):
        skill=(Path(__file__).resolve().parents[1]/'integrations/hermes/skills/expert-review/SKILL.md').read_text()
        for phrase in ('market_query=小3.5','不能替代期间和方向','total_quotes=0','complete_for_filter=true','同一零库存'):
            self.assertIn(phrase,skill)

    async def test_system_prompt_declares_composed_filter_reuse(self):
        from app.hermes_client import SYSTEM_PROMPT
        self.assertIn('合并读盘可携带原文明示的market_query',SYSTEM_PROMPT)
        self.assertIn('完整零库存',SYSTEM_PROMPT)
