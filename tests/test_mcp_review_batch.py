"""Actual MCP transport, bounded parallel native calls, no live upstream."""
import asyncio
import json
import unittest

import httpx
import test_mcp_transport as transport


class ReviewBatchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = transport.MCPTransportTests.asyncSetUp
    asyncTearDown = transport.MCPTransportTests.asyncTearDown
    rpc = transport.MCPTransportTests.rpc

    def item(self, number=0, **changes):
        return dict(team_a=f'Home{number}', team_b=f'Away{number}', sport='football',
            competition_query='来源联赛', from_utc='2026-11-12T00:00:00Z',
            to_utc='2026-11-14T00:00:00Z', market_kind='handicap', period='full_time',
            source_periods=['full_time'], selection_team='team_b', selection_line='+0.5',
            evidence_text=f'Home{number} VS Away{number}，推荐Away{number} +0.5') | changes

    async def install(self, *, mutation=None, failure=None, paginate=False, wait=None):
        calls = []
        current = maximum = 0

        async def upstream(request):
            nonlocal current, maximum
            path = request.url.path.rsplit('/', 1)[-1]
            body = json.loads(request.content)
            calls.append((path, body))
            current += 1
            maximum = max(maximum, current)
            try:
                if wait is not None:
                    wait.set()
                    await asyncio.Event().wait()
                await asyncio.sleep(.002)
                if failure and failure(path, body):
                    return httpx.Response(503, json={'detail': 'isolated unavailable'})
                if path == 'search_matches':
                    number = body['team_a'].removeprefix('Home')
                    result = {'search_key': (number or '0').zfill(64), 'items': [],
                        'search_complete': True, 'next_cursor': None,
                        'resolution': {'resolved': True,
                            'source_team_sides': {'team_a': 'home', 'team_b': 'away'},
                            'fixture': {'id': f'fixture:{number}', 'realtime_eligible': True}}}
                    if paginate and body.get('cursor') is None:
                        result.update(search_complete=False, next_cursor='native-next',
                                      resolution={'resolved': False, 'reason': 'search_not_complete'})
                elif path == 'list_match_markets':
                    fixture = body['fixture_id']
                    rows = [dict(quote_id=f'{fixture}:{side}:{line}', market_kind='handicap',
                        period=body['period'], selection=side, line='-0.5', selection_line=line,
                        status='active', can_record=True) for side, line in
                        [('home', '-0.5'), ('away', '0.5'), ('away', '1.5')]]
                    result = {'fixture': {'id': fixture}, 'markets': rows,
                        'market_filter': {'market_kind': 'handicap', 'period': body['period']},
                        'market_coverage': {'total_quotes': 90, 'matched_quotes': len(rows),
                            'returned_quotes': len(rows), 'complete_for_filter': True}}
                else:
                    self.assertEqual('record_expert_signals', path)
                    result = {'accepted': [dict(signal_id='accepted:'+body['signals'][0]['fixture_id'],
                        price='1.93')], 'rejected': []}
                if mutation is not None:
                    result = mutation(path, body, result)
                return httpx.Response(200, json=result)
            finally:
                current -= 1

        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        return calls, lambda: (current, maximum)

    async def call(self, items=None):
        response = await self.rpc('tools/call', {'name': 'review_match_batch', 'arguments': {
            'review_id': 'job', 'attempt_id': 'active', 'items': items or [self.item()]}})
        self.assertEqual(200, response.status_code, response.text)
        value = response.json()['result']
        self.assertFalse(value.get('isError'), value)
        return json.loads(value['content'][0]['text'])

    async def test_six_sources_are_all_recorded_with_two_native_slots_and_same_attempt(self):
        calls, concurrency = await self.install()
        result = await self.call([self.item(n) for n in range(6)])
        self.assertEqual(list(range(6)), [row['index'] for row in result['items']])
        self.assertTrue(all(row['result']['record_result']['accepted'] for row in result['items']))
        self.assertEqual(6, sum(path == 'record_expert_signals' for path, _ in calls))
        self.assertLessEqual(concurrency()[1], 2)
        self.assertEqual(0, concurrency()[0])
        self.assertTrue(all(body['review_id'] == 'job' and body['attempt_id'] == 'active'
                            for _, body in calls))
        for path, body in calls:
            if path == 'search_matches':
                self.assertEqual('来源联赛', body['competition_query'])
                self.assertEqual('all', body['phase'])
            elif path == 'record_expert_signals':
                self.assertTrue(body['signals'][0]['quote_id'].endswith(':away:0.5'))

    async def test_selected_team_uses_native_orientation_not_source_order(self):
        def swap(path, body, result):
            if path == 'search_matches':
                result['resolution']['source_team_sides'] = {'team_a': 'away', 'team_b': 'home'}
            return result
        calls, _ = await self.install(mutation=swap)
        await self.call([self.item(selection_team='team_a')])
        recorded = [body for path, body in calls if path == 'record_expert_signals']
        self.assertEqual(1, len(recorded))
        self.assertTrue(recorded[0]['signals'][0]['quote_id'].endswith(':away:0.5'))

    async def test_native_selected_line_is_not_provider_home_line_or_neighbor(self):
        calls, _ = await self.install()
        await self.call([self.item(selection_line='1.5', evidence_text='推荐Away0 +1.5')])
        quote = next(body for path, body in calls if path == 'record_expert_signals')['signals'][0]['quote_id']
        self.assertTrue(quote.endswith(':away:1.5'))
        markets = next(body for path, body in calls if path == 'list_match_markets')
        self.assertNotIn('line', markets)
        self.assertNotIn('market_query', markets)

    async def test_every_native_cursor_is_read_before_markets_and_record(self):
        calls, _ = await self.install(paginate=True)
        await self.call()
        self.assertEqual(['search_matches', 'search_matches', 'list_match_markets',
                          'record_expert_signals'], [path for path, _ in calls])
        self.assertIsNone(calls[0][1]['cursor'])
        self.assertEqual('native-next', calls[1][1]['cursor'])

    async def test_pagination_preserves_candidates_from_every_page(self):
        def page(path, body, result):
            if path == 'search_matches': result['items'] = [{'id': body.get('cursor') or 'first'}]
            return result
        calls, _ = await self.install(paginate=True, mutation=page)
        result = await self.call()
        self.assertEqual([{'id': 'first'}, {'id': 'native-next'}], result['items'][0]['result']['items'])
        self.assertEqual(2, result['items'][0]['result']['batch_candidate_pages'])

    async def test_page_failure_preserves_native_resume_cursor_and_other_sources(self):
        calls, _ = await self.install(paginate=True, failure=lambda path, body:
            path == 'search_matches' and body['team_a'] == 'Home0' and body.get('cursor'))
        result = await self.call([self.item(), self.item(1)])
        failed = result['items'][0]
        self.assertEqual('search_matches', failed['failed_operation'])
        self.assertFalse(failed['completed_receipt']['search_complete'])
        self.assertEqual('native-next', failed['completed_receipt']['next_cursor'])
        self.assertTrue(result['items'][1]['result']['record_result']['accepted'])

    async def test_record_unknown_preserves_market_receipt_without_fabricating_acceptance(self):
        calls, _ = await self.install(failure=lambda path, body: path == 'record_expert_signals')
        result = await self.call()
        failed = result['items'][0]
        self.assertEqual('record_expert_signals', failed['failed_operation'])
        self.assertFalse(failed['record_result_verified'])
        self.assertIn('market_result', failed['completed_receipt'])
        self.assertNotIn('record_result', failed['completed_receipt'])

    async def test_total_reuses_native_exact_scope_and_system_price(self):
        def total(path, body, result):
            if path == 'list_match_markets':
                result['markets'] = [dict(quote_id='total-source', market_kind='total',
                    period='full_time', selection='over', line='2.75', selection_line='2.75', status='active', can_record=True)]
                result['market_filter'] = dict(market_kind='total', period='full_time', line='2.75', selection='over')
                result['market_coverage'].update(matched_quotes=1, returned_quotes=1)
            return result
        calls, _ = await self.install(mutation=total)
        result = await self.call([self.item(market_kind='total', market_query='大2.5/3',
            selection_team=None, selection_line=None, evidence_text='全场大2.5/3 @9.99')])
        self.assertEqual('1.93', result['items'][0]['result']['record_result']['accepted'][0]['price'])
        self.assertEqual('total-source', next(b for p, b in calls if p == 'record_expert_signals')['signals'][0]['quote_id'])

    async def test_one_unavailable_source_keeps_other_committed_receipts(self):
        calls, _ = await self.install(failure=lambda path, body:
            path == 'search_matches' and body['team_a'] == 'Home1')
        result = await self.call([self.item(n) for n in range(3)])
        self.assertTrue(result['items'][1]['unresolved'])
        for index in (0, 2):
            self.assertTrue(result['items'][index]['result']['record_result']['accepted'])
        self.assertEqual(2, sum(path == 'record_expert_signals' for path, _ in calls))

    async def test_ambiguous_or_finished_binding_never_reads_inventory(self):
        for replacement in ({'resolved': False, 'reason': 'ambiguous_fixture'},
                            {'resolved': True, 'fixture': {'id': 'fixture:0', 'realtime_eligible': False}}):
            with self.subTest(resolution=replacement):
                def mutate(path, body, result):
                    if path == 'search_matches': result['resolution'] = replacement
                    return result
                calls, _ = await self.install(mutation=mutate)
                await self.call()
                self.assertEqual(['search_matches'], [path for path, _ in calls])

    async def test_unknown_or_ambiguous_side_mapping_never_records(self):
        for sides in (None, {}, {'team_a': 'home'}, {'team_a': 'home', 'team_b': 'home'}):
            with self.subTest(sides=sides):
                def mutate(path, body, result):
                    if path == 'search_matches': result['resolution']['source_team_sides'] = sides
                    return result
                calls, _ = await self.install(mutation=mutate)
                await self.call()
                self.assertNotIn('record_expert_signals', [path for path, _ in calls])

    async def test_no_record_on_incomplete_duplicate_stale_wrong_fixture_or_period_rows(self):
        mutations = [lambda r: r['market_coverage'].update(complete_for_filter=False),
            lambda r: r['markets'].append(dict(r['markets'][1], quote_id='duplicate')),
            lambda r: r['markets'][1].update(can_record=False),
            lambda r: r['markets'][1].update(status='suspended'),
            lambda r: r['markets'][1].update(fixture_id='other'),
            lambda r: r['markets'][1].update(period='first_half'),
            lambda r: r['market_filter'].update(line='-0.5'),
            lambda r: r['markets'][1].update(selection_line='NaN')]
        for mutation in mutations:
            def mutate(path, body, result):
                if path == 'list_match_markets': mutation(result)
                return result
            calls, _ = await self.install(mutation=mutate)
            await self.call()
            self.assertNotIn('record_expert_signals', [path for path, _ in calls])

    async def test_unknown_period_duplicate_pair_extra_authority_and_budget_fail_before_any_call(self):
        invalid = [[self.item(source_periods=[])], [self.item(period='unknown')],
            [self.item(source_periods=['first_half'])], [self.item(selection_team=None)],
            [self.item(selection_line=None)], [self.item(selection_line='.3')],
            [self.item(selection_line='NaN')], [self.item(market_query='Away0+0.5')],
            [self.item(review_id='different')], [self.item(), self.item()],
            [self.item(n) for n in range(7)]]
        for items in invalid:
            with self.subTest(items=items):
                calls, _ = await self.install()
                response = await self.rpc('tools/call', {'name': 'review_match_batch', 'arguments': {
                    'review_id': 'job', 'attempt_id': 'active', 'items': items}})
                self.assertTrue(response.json()['result'].get('isError'))
                self.assertEqual([], calls)

    async def test_source_league_must_be_declared_before_any_lookup(self):
        calls, _ = await self.install()
        item = self.item()
        del item['competition_query']
        response = await self.rpc('tools/call', {'name': 'review_match_batch', 'arguments': {
            'review_id': 'job', 'attempt_id': 'active', 'items': [item]}})
        self.assertTrue(response.json()['result'].get('isError'))
        self.assertEqual([], calls)
        tools = (await self.rpc('tools/list')).json()['result']['tools']
        schema = next(tool for tool in tools if tool['name'] == 'review_match_batch')['inputSchema']
        self.assertIn('competition_query', schema['$defs']['SourceMatchIntent']['required'])
        for token in ('handicap', 'null'):
            self.assertIn(token, schema['$defs']['SourceMatchIntent']['properties']['market_query']['description'])

    async def test_empty_optional_query_is_absence_without_losing_handicap_scope(self):
        for empty in ('', '  \t'):
            calls, _ = await self.install()
            result = await self.call([self.item(market_query=empty)])
            self.assertTrue(result['items'][0]['result']['record_result']['accepted'])
            market = next(body for path, body in calls if path == 'list_match_markets')
            self.assertNotIn('market_query', market)
            recorded = next(body for path, body in calls if path == 'record_expert_signals')
            self.assertTrue(recorded['signals'][0]['quote_id'].endswith(':away:0.5'))

    async def test_empty_total_query_does_not_guess_a_source_line(self):
        for empty in ('', '  \t'):
            calls, _ = await self.install()
            response = await self.rpc('tools/call', {'name': 'review_match_batch', 'arguments': {
                'review_id': 'job', 'attempt_id': 'active', 'items': [self.item(
                    market_kind='total', market_query=empty, selection_team=None, selection_line=None)]}})
            self.assertTrue(response.json()['result'].get('isError'))
            self.assertEqual([], calls)

    async def test_cancellation_drains_active_and_waiting_items(self):
        entered = asyncio.Event()
        calls, concurrency = await self.install(wait=entered)
        task = asyncio.create_task(self.server.review_match_batch('job', 'active',
            [self.server.SourceMatchIntent(**self.item(n)) for n in range(6)]))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(0, concurrency()[0])
        self.assertLessEqual(len(calls), 2)

    async def test_pagination_budget_keeps_native_incomplete_receipt_without_inventory(self):
        def incomplete(path, body, result):
            if path == 'search_matches':
                result.update(search_complete=False, next_cursor='native-next',
                              resolution={'resolved': False})
            return result
        calls, _ = await self.install(mutation=incomplete)
        result = await self.call()
        self.assertEqual(20, len(calls))
        receipt = result['items'][0]['result']
        self.assertFalse(receipt['search_complete'])
        self.assertEqual('native-next', receipt['next_cursor'])
        self.assertEqual('pagination_budget_exceeded', receipt['batch_unresolved'])
