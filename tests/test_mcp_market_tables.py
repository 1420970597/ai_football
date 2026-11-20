"""An opt-in model presentation must preserve every native quote cell and guard."""
import asyncio
import copy
import json
import unittest

import httpx
from mcp.types import CallToolResult, TextContent
import test_mcp_transport as support


class MarketTableTransportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.MCPTransportTests.asyncSetUp
    asyncTearDown = support.MCPTransportTests.asyncTearDown
    rpc = support.MCPTransportTests.rpc

    def native(self, count=12):
        return {'fixture': {'id':'leyu:fixture','phase':'live'},
                'market_filter': {'market_kind':'handicap','period':'full_time'},
                'market_coverage': {'total_quotes':count,'matched_quotes':count,'returned_quotes':count,'complete_for_filter':True},
                'refresh': {'attempted':True,'accepted':True,'reason':'refresh_completed'},
                'markets': [{'quote_id':f'isolated-quote-{i}','market_name':'全场让分','market_kind':'handicap',
                    'period':'full_time','line':'2.500','selection_line':'-2.500' if i%2 else '2.500',
                    'selection':'away'if i%2 else'home','line_basis':'provider_home',
                    'score_basis':'full_period','status':'closed'if i==0 else'active',
                    'fresh':i%3==0,'confirmed_current':i%3!=0,'can_record':i!=0}
                    for i in range(count)]}

    def restore(self, value):
        if 'market_result'in value:
            return value|{'market_result':self.restore(value['market_result'])}
        table=value.get('market_table')
        self.assertIsNotNone(table,'explicit table profile must compact a uniform large native quote list')
        self.assertEqual('column-rows-v1',table['encoding'])
        self.assertEqual(len(table['columns']),len(set(table['columns'])))
        rows=[]
        for row in table['rows']:
            self.assertEqual(len(table['columns']),len(row))
            rows.append(dict(zip(table['columns'],row,strict=True)))
        return {k:v for k,v in value.items()if k!='market_table'}|{'markets':rows}

    async def install(self, native):
        captured=[]
        async def upstream(request):
            await asyncio.sleep(0)
            captured.append(json.loads(request.content))
            return httpx.Response(200,json=native)
        await self.server.client.aclose()
        self.server.client=httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        return captured

    async def read(self, profile=None):
        headers={}if profile is None else {'X-Football-Tool-Profile':profile}
        r=await self.client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
            'name':'list_match_markets','arguments':{'review_id':'review','attempt_id':'attempt','fixture_id':'leyu:fixture'}}})
        self.assertEqual(200,r.status_code)
        body=r.json()['result'];self.assertFalse(body.get('isError',False))
        return json.loads(body['content'][0]['text'])

    async def test_opt_in_real_rpc_preserves_every_cell_and_business_request(self):
        native=self.native();original=copy.deepcopy(native);captured=await self.install(native)
        result=await self.read('market-table-v1')
        self.assertEqual(native,self.restore(result));self.assertEqual(original,native)
        self.assertEqual(1,len(captured));self.assertEqual(('review','attempt','leyu:fixture'),tuple(captured[0][k]for k in('review_id','attempt_id','fixture_id')))
        self.assertLess(len(json.dumps(result,ensure_ascii=False)),len(json.dumps(native,ensure_ascii=False))*.8)

    async def test_default_unknown_and_existing_profiles_keep_native_shapes(self):
        native=self.native();await self.install(native)
        for profile in(None,'unknown-v1','parallel-compact-v1'):
            with self.subTest(profile=profile):self.assertEqual(native,await self.read(profile))

    async def test_parallel_profile_and_concurrent_default_context_are_independent(self):
        native=self.native();await self.install(native)
        compact,default=await asyncio.gather(self.read('parallel-table-v1'),self.read())
        self.assertEqual(native,self.restore(compact));self.assertEqual(native,default)
        self.assertEqual(native,await self.read())

    async def test_empty_small_and_heterogeneous_inventory_keep_original_contract(self):
        for count in(0,1,7):
            native=self.native(count);await self.install(native);self.assertEqual(native,await self.read('market-table-v1'))
        native=self.native();del native['markets'][0]['line'];await self.install(native)
        self.assertEqual(native,await self.read('market-table-v1'))

    async def test_closed_invalid_and_nullable_cells_are_not_removed_or_promoted(self):
        native=self.native()
        for i,row in enumerate(native['markets']):row['source_context']=None if i%2 else {'text':'原文 "\\n"  +0.5/1','valid':False}
        await self.install(native);restored=self.restore(await self.read('market-table-v1'))
        self.assertEqual(native,restored);self.assertFalse(restored['markets'][0]['can_record'])
        self.assertEqual('closed',restored['markets'][0]['status'])

    async def test_profile_never_changes_tool_schemas_or_authentication(self):
        default=(await self.rpc('tools/list')).json()['result']['tools']
        result=await self.client.post('/mcp',headers={'X-Football-Tool-Profile':'market-table-v1'},json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
        self.assertEqual(default,result.json()['result']['tools'])
        forbidden=await self.client.post('/mcp',headers={'X-Football-Tool-Profile':'market-table-v1','Authorization':'Bearer invalid'},json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
        self.assertEqual(401,forbidden.status_code)

    async def test_errors_remain_errors_without_table_or_retry(self):
        calls=[]
        def upstream(request):calls.append(request);return httpx.Response(409,json={'detail':'租约失效'})
        await self.server.client.aclose();self.server.client=httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        r=await self.client.post('/mcp',headers={'X-Football-Tool-Profile':'market-table-v1'},json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
            'name':'list_match_markets','arguments':{'review_id':'r','attempt_id':'a','fixture_id':'f'}}})
        result=r.json()['result'];self.assertTrue(result['isError']);self.assertIn('租约失效',result['content'][0]['text']);self.assertEqual(1,len(calls))

    async def test_composed_native_market_result_has_identical_search_binding_and_quotes(self):
        native=self.native();composed={'search_key':'a'*64,'search_complete':True,'next_cursor':None,'resolution':{'resolved':True},'market_result':native}
        await self.install(composed);result=await self.read('market-table-v1')
        self.assertEqual(composed,self.restore(result))
