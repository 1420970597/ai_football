"""Omitted duplicate search text reuses explicit source teams, never identities."""
import unittest
import test_mcp_composed_query as support


class MCPQueryEfficiencyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ComposedMarketQueryTests.asyncSetUp
    asyncTearDown = support.ComposedMarketQueryTests.asyncTearDown
    rpc = support.ComposedMarketQueryTests.rpc
    install = support.ComposedMarketQueryTests.install

    async def pair(self, **updates):
        arguments={'review_id':'job','attempt_id':'attempt','team_a':'source home',
                   'team_b':'source away','read_markets':True,'market_kind':'handicap',
                   'period':'full_time'}
        arguments.update(updates)
        response=await self.rpc('tools/call',{'name':'search_matches','arguments':arguments})
        self.assertEqual(200,response.status_code)
        return response.json()['result']

    async def test_schema_allows_omitting_duplicate_query_but_keeps_source_pair(self):
        tools=(await self.rpc('tools/list')).json()['result']['tools']
        schema=next(t for t in tools if t['name']=='search_matches')['inputSchema']
        self.assertNotIn('team_query',schema.get('required',[]))
        self.assertEqual({'review_id','attempt_id'},set(schema.get('required',[])))

    async def test_explicit_pair_without_query_uses_exact_first_source_team_once(self):
        calls=await self.install();result=await self.pair()
        self.assertFalse(result.get('isError',False),result)
        self.assertEqual(2,len(calls))
        self.assertEqual('source home',calls[0][1]['team_query'])
        self.assertEqual(('source home','source away'),
                         (calls[0][1]['team_a'],calls[0][1]['team_b']))
        self.assertEqual({'job'},{body['review_id']for _,body in calls})
        self.assertEqual({'attempt'},{body['attempt_id']for _,body in calls})

    async def test_explicit_empty_or_different_query_is_never_overwritten(self):
        for query in ('','source fragment'):
            with self.subTest(query=query):
                calls=await self.install();result=await self.pair(team_query=query)
                self.assertFalse(result.get('isError',False),result)
                self.assertEqual(query,calls[0][1]['team_query'])

    async def test_missing_query_and_incomplete_or_blank_pair_make_no_business_request(self):
        for updates in ({'team_a':None},{'team_b':None},{'team_a':' '},{'team_b':'\t'}):
            with self.subTest(updates=updates):
                calls=await self.install();result=await self.pair(**updates)
                self.assertTrue(result.get('isError'),result)
                self.assertEqual([],calls)

    async def test_null_query_reuses_explicit_pair_without_inventing_candidate_id(self):
        calls=await self.install();result=await self.pair(team_query=None)
        self.assertFalse(result.get('isError',False),result)
        self.assertEqual('source home',calls[0][1]['team_query'])
        self.assertNotIn('fixture_id',calls[0][1])

    async def test_pair_fallback_does_not_read_markets_before_pagination_completes(self):
        calls=await self.install(complete=False);result=await self.pair()
        self.assertFalse(result.get('isError',False),result)
        self.assertEqual(1,len(calls))
        self.assertEqual('source home',calls[0][1]['team_query'])

    async def test_market_tool_explains_complete_empty_inventory_stop_rule(self):
        tools=(await self.rpc('tools/list')).json()['result']['tools']
        description=next(t for t in tools if t['name']=='list_match_markets')['description']
        self.assertIn('total_quotes=0',description)
        self.assertIn('complete_for_filter=true',description)
        self.assertNotIn('零结果放宽筛选',description)
