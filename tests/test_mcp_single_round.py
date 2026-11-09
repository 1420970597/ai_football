"""A precise source intent may reuse guarded operations in one MCP call."""
import asyncio
import json
import unittest

import httpx
import test_mcp_transport as transport


class SingleRoundRecordTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = transport.MCPTransportTests.asyncSetUp
    asyncTearDown = transport.MCPTransportTests.asyncTearDown
    rpc = transport.MCPTransportTests.rpc

    def arguments(self, name='search_matches', **changes):
        args = {'review_id': 'job', 'attempt_id': 'current', 'team_a': 'Alpha', 'team_b': 'Beta',
                'read_markets': True, 'market_kind': 'total', 'period': 'first_half',
                'market_query': '1.5大', 'record_source_intent': True,
                'evidence_text': '原文上半场1.5大 @9.99'}
        args.update({'search_key': 'a'*64} if name == 'resolve_match' else {'team_query': 'Alpha'})
        return args | changes

    def quote(self, **changes):
        return {'quote_id': 'b2222222-2222-4222-8222-222222222222', 'fixture_id': 'leyu:123456',
                'market_kind': 'total', 'market_id': '18', 'period': 'first_half',
                'line': '1.500', 'selection': 'over', 'status': 'active', 'can_record': True} | changes

    async def install(self, *, quotes=None, coverage=None, filters=None,
                      resolution=None, complete=True, failure=None, record_wait=None, record_result=None):
        calls = []
        quotes = [self.quote()] if quotes is None else quotes
        markets = {'markets': quotes,
                   'market_filter': {'market_kind': 'total', 'period': 'first_half',
                                     'line': '1.5', 'selection': 'over'} if filters is None else filters,
                   'market_coverage': {'total_quotes': 30, 'matched_quotes': len(quotes),
                                       'returned_quotes': len(quotes), 'complete_for_filter': True}
                       if coverage is None else coverage}
        resolved = {'resolved': True, 'fixture': {'id': 'leyu:123456', 'realtime_eligible': True}}
        resolved = resolved if resolution is None else resolution

        async def upstream(request):
            path = request.url.path.rsplit('/', 1)[-1]
            body = json.loads(request.content)
            calls.append((path, body))
            if failure == path:
                return httpx.Response(503, json={'detail': 'isolated upstream unavailable'})
            if path == 'list_match_markets':
                return httpx.Response(200, json=markets)
            if path == 'record_expert_signals':
                if record_wait is not None:
                    record_wait.set()
                    await asyncio.Event().wait()
                return httpx.Response(200, json={'accepted': [{'signal_id': 'signal', 'price': '1.95000'}],
                                                'rejected': []} if record_result is None else record_result)
            value = resolved if path == 'resolve_match' else {
                'search_key': 'a'*64, 'search_complete': complete,
                'next_cursor': None if complete else 'next-page', 'resolution': resolved}
            return httpx.Response(200, json=value)

        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        return calls

    async def call(self, name='search_matches', **changes):
        response = await self.rpc('tools/call', {'name': name, 'arguments': self.arguments(name, **changes)})
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()['result']
        self.assertFalse(result.get('isError', False), result)
        return json.loads(result['content'][0]['text'])

    async def test_opt_in_lookup_records_the_unique_read_quote_at_system_price(self):
        for name in ('search_matches', 'resolve_match'):
            with self.subTest(name=name):
                calls = await self.install()
                result = await self.call(name)
                self.assertEqual([name, 'list_match_markets', 'record_expert_signals'], [p for p, _ in calls])
                self.assertEqual('1.95000', result['record_result']['accepted'][0]['price'])
                self.assertTrue(result['source_intent_recording']['attempted'])
                self.assertEqual('1.5大', calls[1][1]['market_query'])
                record = calls[2][1]
                self.assertEqual(('job', 'current'), (record['review_id'], record['attempt_id']))
                self.assertEqual({'fixture_id': 'leyu:123456', 'quote_id': self.quote()['quote_id'],
                    'evidence_text': '原文上半场1.5大 @9.99', 'confidence': .5}, record['signals'][0])
                for _, body in calls[:2]:
                    self.assertNotIn('record_source_intent', body)
                    self.assertNotIn('evidence_text', body)

    async def test_legacy_read_is_not_a_recording_request(self):
        calls = await self.install()
        args = self.arguments()
        args.pop('record_source_intent'); args.pop('evidence_text')
        response = await self.rpc('tools/call', {'name': 'search_matches', 'arguments': args})
        result = response.json()['result']
        self.assertFalse(result.get('isError', False), result)
        value = json.loads(result['content'][0]['text'])
        self.assertNotIn('record_result', value)
        self.assertEqual(['search_matches', 'list_match_markets'], [p for p, _ in calls])

    async def test_invalid_fast_path_request_fails_before_any_lookup(self):
        for changes in ({'read_markets': False}, {'period': None}, {'period': 'unknown'},
                        {'market_kind': 'handicap'}, {'market_query': None},
                        {'market_query': ''}, {'evidence_text': None}, {'evidence_text': '   '}):
            with self.subTest(changes=changes):
                calls = await self.install()
                response = await self.rpc('tools/call', {'name': 'search_matches',
                                                       'arguments': self.arguments(**changes)})
                self.assertTrue(response.json()['result'].get('isError'))
                self.assertEqual([], calls)

    async def test_only_complete_unique_eligible_native_scope_can_be_recorded(self):
        cases = [{'quotes': []}, {'quotes': [self.quote(), self.quote(quote_id='other')]},
                 {'quotes': [self.quote(can_record=False)]}, {'quotes': [self.quote(can_record='true')]},
                 {'quotes': [self.quote(status='closed')]}, {'quotes': [self.quote(fixture_id='other')]},
                 {'quotes': [self.quote(period='full_time')]}, {'quotes': [self.quote(selection='under')]},
                 {'quotes': [self.quote(market_kind='team_total')]}, {'quotes': [self.quote(line='1.75')]},
                 {'quotes': [self.quote(line='NaN')]}, {'quotes': [self.quote(quote_id='')]},
                 {'filters': {'market_kind': 'total', 'period': 'first_half', 'selection': 'over'}},
                 {'filters': {'market_kind': 'total', 'period': None, 'line': '1.5', 'selection': 'over'}},
                 {'coverage': {'total_quotes': 30, 'matched_quotes': 1, 'returned_quotes': 1,
                               'complete_for_filter': False}},
                 {'coverage': {'total_quotes': 30, 'matched_quotes': 2, 'returned_quotes': 1,
                               'complete_for_filter': True}},
                 {'coverage': {'total_quotes': 0, 'matched_quotes': 1, 'returned_quotes': 1,
                               'complete_for_filter': True}}]
        for case in cases:
            with self.subTest(case=case):
                calls = await self.install(**case)
                value = await self.call()
                self.assertNotIn('record_result', value)
                self.assertFalse(value['source_intent_recording']['attempted'])
                self.assertIn('market_result', value)
                self.assertNotIn('record_expert_signals', [p for p, _ in calls])

    async def test_pagination_ambiguity_or_finished_binding_never_reads_or_records(self):
        for settings in ({'complete': False}, {'resolution': {'resolved': False}},
                         {'resolution': {'resolved': True, 'fixture': {
                             'id': 'leyu:123456', 'realtime_eligible': False}}}):
            with self.subTest(settings=settings):
                calls = await self.install(**settings)
                value = await self.call()
                self.assertNotIn('record_result', value)
                self.assertEqual(['search_matches'], [p for p, _ in calls])

    async def test_failed_record_preserves_completed_receipt_without_claiming_acceptance(self):
        calls = await self.install(failure='record_expert_signals')
        response = await self.rpc('tools/call', {'name': 'search_matches', 'arguments': self.arguments()})
        result = response.json()['result']
        self.assertTrue(result.get('isError'))
        content = result['content'][0]['text']
        error = json.loads(content[content.index('{'):])
        self.assertEqual('record_expert_signals', error['failed_operation'])
        receipt = error['completed_receipt']
        self.assertEqual(('job', 'current'), (receipt['review_id'], receipt['attempt_id']))
        self.assertEqual(self.quote()['quote_id'], receipt['market_result']['markets'][0]['quote_id'])
        self.assertNotIn('record_result', error)
        self.assertEqual(['search_matches', 'list_match_markets', 'record_expert_signals'], [p for p, _ in calls])

    async def test_failed_market_read_cannot_submit_a_pick(self):
        calls = await self.install(failure='list_match_markets')
        response = await self.rpc('tools/call', {'name': 'search_matches', 'arguments': self.arguments()})
        result = response.json()['result']
        self.assertTrue(result.get('isError'))
        self.assertNotIn('record_expert_signals', [p for p, _ in calls])

    async def test_native_rejection_is_returned_as_unresolved_without_resubmission(self):
        rejection={'accepted':[],'rejected':[{'index':0,'reason':'source_period_intent_conflict'}]}
        calls=await self.install(record_result=rejection)
        result=await self.call()
        self.assertEqual(rejection,result['record_result'])
        self.assertTrue(result['source_intent_recording']['attempted'])
        self.assertEqual(1,sum(path=='record_expert_signals'for path,_ in calls))

    async def test_invalid_native_record_receipt_is_not_success(self):
        await self.install(record_result={})
        response=await self.rpc('tools/call',{'name':'search_matches','arguments':self.arguments()})
        result=response.json()['result']
        self.assertTrue(result.get('isError'))
        content=result['content'][0]['text'];error=json.loads(content[content.index('{'):])
        self.assertEqual('record_expert_signals',error['failed_operation'])
        self.assertFalse(error['record_result_verified'])

    async def test_cancelling_composed_record_releases_native_admission(self):
        entered = asyncio.Event()
        await self.install(record_wait=entered)
        task = asyncio.create_task(self.server.search_matches(**self.arguments()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(0, self.server.pending_market_calls)
        self.assertEqual(0, self.server.pending_market_requests)
        self.assertFalse(self.server.market_gate.locked())
        self.assertEqual({}, self.server.market_reads)
