import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import httpx
from fastapi import FastAPI
from app import database
from app.routes import public
import test_reviews as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires task-owned isolated database')
class AsyncScoreSummaryTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown=support.ReviewTests.asyncTearDown
    fixture=support.ReviewTests.fixture

    async def asyncSetUp(self):
        await support.ReviewTests.asyncSetUp(self)
        await self.client.aclose()
        self.app=FastAPI();self.app.state.settings=SimpleNamespace(session_secret='isolated-cursor-only',quote_max_age_seconds=60)
        self.app.include_router(public.router)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test')

    async def test_facts_request_never_reads_optional_quotes(self):
        fixture=await self.fixture(1,phase='live')
        original=self.pool
        class Guard:
            async def fetch(self,sql,*args):
                if 'v2_current_quotes' in sql:raise AssertionError('optional quote query blocked match facts')
                return await original.fetch(sql,*args)
            def __getattr__(self,key):return getattr(original,key)
        with patch.object(database,'pool',Guard()):
            response=await self.client.get('/api/v1/public/matches',params={'phase':'live','include_markets':'false'})
        self.assertEqual(200,response.status_code,response.text)
        data=response.json();self.assertFalse(data['markets_included'])
        self.assertEqual([fixture],[m['id']for m in data['items']])
        self.assertEqual('not_requested',data['items'][0]['markets_status'])
        self.assertEqual([],data['items'][0]['markets'])

    async def test_summary_preserves_exact_default_quote_contract_and_selected_limit(self):
        await self.fixture(1,markets=[dict(market_id='total',market_name='全场大小',kind='total',period='full_time',
            line=str(2+i/4),selection='over',price='1.95',source_updated_at=self.now.isoformat(),
            received_at=self.now.isoformat())for i in range(30)])
        old=(await self.client.get('/api/v1/public/matches',params={'phase':'scheduled'})).json()['items'][0]
        response=await self.client.get('/api/v1/public/match-summaries',params={'fixture_ids':old['id']})
        self.assertEqual(200,response.status_code,response.text)
        item=response.json()['items'][0]
        self.assertEqual(old['markets'],item['markets'])
        self.assertEqual(24,len(item['markets']))
        self.assertEqual(old['quote_fresh'],item['quote_fresh'])
        self.assertEqual('loaded',item['markets_status'])

    async def test_row_shape_returns_exactly_the_quotes_the_row_draws(self):
        quotes=[dict(market_id='total',market_name='全场大小',kind='total',period='full_time',
            line=str(2+i/4),selection=selection,price='1.95',source_updated_at=self.now.isoformat(),
            received_at=self.now.isoformat())for i in range(30)for selection in ('over','under')]
        fixture=await self.fixture(1,markets=quotes)
        full=(await self.client.get('/api/v1/public/match-summaries',params={'fixture_ids':fixture})).json()['items'][0]
        row=(await self.client.get('/api/v1/public/match-summaries',
            params={'fixture_ids':fixture,'mode':'row'})).json()
        self.assertEqual('row',row['shape'])
        item=row['items'][0]
        # The row shows the main market line and at most three of its selections.
        self.assertLessEqual(len(item['markets']),3)
        # markets_total counts every current quote, not just the ones shipped.
        self.assertEqual(len(quotes),item['markets_total'])
        self.assertEqual(24,len(full['markets']))
        # The row shape is a prefix of the full size in the same order, so the
        # row never shows a quote the full shape would not have shown.
        self.assertEqual(full['markets'][:len(item['markets'])],item['markets'])
        self.assertEqual(full['quote_fresh'],item['quote_fresh'])
        self.assertEqual('loaded',item['markets_status'])

    async def test_row_shape_rejects_unknown_mode(self):
        fixture=await self.fixture(1)
        response=await self.client.get('/api/v1/public/match-summaries',
            params={'fixture_ids':fixture,'mode':'everything'})
        self.assertEqual(422,response.status_code,response.text)

    async def test_empty_finished_and_unknown_fixtures_remain_distinct(self):
        fixture=await self.fixture(1,phase='finished',markets=[])
        response=await self.client.get('/api/v1/public/match-summaries',params=[('fixture_ids',fixture),('fixture_ids','unknown')])
        self.assertEqual(200,response.status_code,response.text)
        data=response.json();self.assertEqual(['unknown'],data['missing_fixture_ids'])
        self.assertEqual([fixture],[i['fixture_id']for i in data['items']])
        self.assertEqual([],data['items'][0]['markets'])
        self.assertFalse(data['items'][0]['quote_fresh'])
        self.assertEqual('finished',data['items'][0]['phase'])

    async def test_requested_ids_are_bounded_and_validated(self):
        for params in ([],[('fixture_ids','')],[('fixture_ids','x'*121)],
                       [('fixture_ids',str(i))for i in range(51)]):
            response=await self.client.get('/api/v1/public/match-summaries',params=params)
            self.assertEqual(422,response.status_code,response.text)

    async def test_duplicate_ids_are_deduplicated_and_endpoint_is_read_only(self):
        fixture=await self.fixture()
        response=await self.client.get('/api/v1/public/match-summaries',params=[('fixture_ids',fixture)]*2)
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual([fixture],[i['fixture_id']for i in response.json()['items']])
        for table in ('v2_expert_signals','v2_bet_orders','v2_analysis_traces'):
            self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM '+table))
