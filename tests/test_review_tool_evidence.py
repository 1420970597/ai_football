"""Real isolated PostgreSQL/API evidence; no providers, accounts or wagers."""
from datetime import timedelta
import asyncio
import base64
import hashlib
import json
import os
import unittest
from unittest.mock import AsyncMock, patch
import uuid
from types import SimpleNamespace
import httpx

from fastapi import HTTPException

from app.review_tool_evidence import (
    CHUNK_BYTES, MAX_SNAPSHOT_BYTES, encode_snapshot, replay_snapshot, snapshot_result,
)
from app.review_state import append_traces
from app.routes import reviews
from app import review_tool_evidence
from tests import test_reviews as fixtures
from tests import test_mcp_transport as mcp_fixtures


class _CountingConnection:
    def __init__(self, connection, pool):
        self._connection = connection
        self._pool = pool

    def __getattr__(self, name):
        value = getattr(self._connection, name)
        if name not in {'execute', 'fetch', 'fetchrow', 'fetchval'}:
            return value

        async def counted(*args, **kwargs):
            self._pool.commands += 1
            return await value(*args, **kwargs)

        return counted


class _CountingAcquire:
    def __init__(self, acquire, pool):
        self._acquire = acquire
        self._pool = pool

    async def __aenter__(self):
        connection = await self._acquire.__aenter__()
        return _CountingConnection(connection, self._pool)

    async def __aexit__(self, *args):
        return await self._acquire.__aexit__(*args)


class _CountingPool:
    def __init__(self, pool):
        self._pool = pool
        self.commands = 0

    def acquire(self):
        return _CountingAcquire(self._pool.acquire(), self)


class ToolSnapshotUnitTests(unittest.TestCase):
    def test_large_market_inventory_keeps_all_supported_quotes_with_explicit_coverage(self):
        quote = {'quote_id': str(uuid.uuid4()), 'market_id': 'total', 'market_name': '全场大小附加盘口' * 5,
                 'period': 'full_time', 'market_kind': 'total', 'line': '2.500', 'selection': 'over',
                 'price': '1.95000', 'status': 'active', 'is_main': False, 'score_basis': 'remaining',
                 'source_updated_at': '2026-11-01T17:15:40+00:00', 'fresh': True,
                 'confirmed_current': True, 'can_record': True}
        supported = [quote | {'quote_id': str(uuid.uuid4()), 'line': str(index / 4)} for index in range(60)]
        unsupported = [quote | {'quote_id': str(uuid.uuid4()), 'market_id': str(1000 + index % 64),
                       'market_kind': 'unsupported', 'status': 'closed', 'can_record': False}
                       for index in range(178)]
        source = {'fixture': {'id': 'fixture'}, 'markets': supported + unsupported, 'refresh': {'attempted': False}}
        before = json.dumps(source, ensure_ascii=False, indent=2)
        wire = snapshot_result('mcp_list_match_markets', source, compact_markets=True)
        after = json.dumps(wire, ensure_ascii=False, indent=2)
        self.assertGreater(len(before), 100000)
        self.assertLess(len(after), 100000)
        self.assertLess(len(after), len(before) * .6)
        compact_supported = [{key: item[key] for key in review_tool_evidence.MODEL_QUOTE_FIELDS if key in item}
                             | {'line_basis':'selection', 'selection_line':item['line']}
                             for item in supported]
        self.assertEqual(compact_supported, wire['markets'])
        self.assertEqual({'total_quotes': 238, 'matched_quotes': 238, 'returned_quotes': 60,
                          'omitted_unsupported_quotes': 178}, wire['market_coverage'])
        self.assertEqual(178, sum(group['quote_count'] for group in wire['unsupported_markets']))
        self.assertEqual(64, len(wire['unsupported_markets']))
        self.assertEqual(238, len(source['markets']))
        self.assertEqual(source, snapshot_result('mcp_list_match_markets', source))
        self.assertEqual(wire, snapshot_result('mcp_list_match_markets', wire))

    def test_only_unsupported_options_return_explicit_unavailable_summary(self):
        source = {'fixture': {'id': 'fixture'}, 'markets': [
            {'quote_id': 'unknown-option', 'market_id': '11', 'market_name': '11',
             'period': 'source_11', 'market_kind': 'unsupported'}], 'refresh': {}}
        wire = snapshot_result('mcp_list_match_markets', source, compact_markets=True)
        self.assertEqual([], wire['markets'])
        self.assertEqual(1, wire['market_coverage']['omitted_unsupported_quotes'])
        self.assertEqual('unsupported_market_kind', wire['unsupported_markets'][0]['reason'])
        self.assertEqual(source, snapshot_result('mcp_list_match_markets', source))

    def test_market_filter_coverage_survives_unsupported_option_compaction(self):
        source = {'fixture': {'id': 'fixture'}, 'markets': [
            {'quote_id': 'unsupported', 'market_id': 'unknown', 'market_name': '未知',
             'period': 'full_time', 'market_kind': 'unsupported', 'status': 'closed'},
        ], 'market_filter': {'market_kind': 'unsupported'},
            'market_coverage': {'total_quotes': 238, 'matched_quotes': 1,
                                'returned_quotes': 1, 'complete_for_filter': True},
            'refresh': {}}
        wire = snapshot_result('mcp_list_match_markets', source, compact_markets=True)
        self.assertEqual({'market_kind': 'unsupported'}, wire['market_filter'])
        self.assertEqual({'total_quotes': 238, 'matched_quotes': 1, 'returned_quotes': 0,
                          'complete_for_filter': True, 'omitted_unsupported_quotes': 1}, wire['market_coverage'])

    def test_supported_only_output_over_inline_budget_is_explicitly_rejected(self):
        source = {'fixture': {'id': 'fixture'}, 'markets': [
            {'quote_id': str(index), 'market_name': '受支持盘口' * 100, 'market_kind': 'total'}
            for index in range(200)], 'refresh': {}}
        with self.assertRaises(HTTPException) as rejected:
            snapshot_result('mcp_list_match_markets', source, compact_markets=True)
        self.assertEqual(413, rejected.exception.status_code)
        self.assertIn('no partial response', rejected.exception.detail)
        self.assertEqual(200, len(snapshot_result('mcp_list_match_markets', source)['markets']))

    def test_compact_market_keeps_every_option_and_trading_semantics(self):
        quote={'id':'native-id','fixture_id':'fixture','quote_id':'model-id',
               'play_id':'protocol-play','market_instance_id':'protocol-instance','selection_id':'protocol-option',
               'received_at':'receipt','source_time_quality':'observed',
               'market_id':'totals','market_name':'上半场大小附加盘','period':'first_half','market_kind':'totals',
               'line':'0.750','selection':'over','price':'1.76000','status':'active','is_main':False,
               'score_basis':'remaining','source_updated_at':'2026-11-01T10:00:00Z',
               'fresh':False,'confirmed_current':True,'can_record':True}
        source={'fixture':{'id':'fixture'},'markets':[quote,quote|{'quote_id':'second','selection':'under'}],
                'refresh':{'attempted':True,'accepted':True,'reason':'refresh_completed'}}
        compact=snapshot_result('mcp_list_match_markets',source,compact_markets=True)
        self.assertEqual(['model-id','second'],[row['quote_id'] for row in compact['markets']])
        for original,projected in zip(source['markets'],compact['markets']):
            self.assertEqual({key:original[key] for key in review_tool_evidence.MODEL_QUOTE_FIELDS if key in original}
                             | {'line_basis':'selection', 'selection_line':original['line']},projected)
        self.assertNotIn('price', compact['markets'][0])
        self.assertNotIn('source_updated_at', compact['markets'][0])
        self.assertLess(len(json.dumps(compact)),len(json.dumps(source)))
        self.assertIn('play_id',source['markets'][0],'input evidence must remain untouched')
        legacy=snapshot_result('mcp_list_match_markets',source)
        self.assertEqual(source,legacy,'legacy response replay keeps every declared field')

    def test_legacy_market_snapshot_replay_does_not_apply_new_compaction(self):
        original={'fixture':{'id':'fixture'},'markets':[{'id':'old-id','quote_id':'old-id','fixture_id':'fixture',
                  'play_id':'old-play','market_instance_id':'old-instance','selection_id':'old-option',
                  'received_at':'original-receipt','source_time_quality':'original-quality'}],
                  'refresh':{'attempted':False}}
        manifest,blocks=encode_snapshot(original)
        manifest['call_id']='original-call'
        chunks=[{'call_id':'original-call','snapshot_sha256':manifest['sha256'],
                 'chunk_index':i,'data_base64':block} for i,block in enumerate(blocks)]
        replayed=snapshot_result('mcp_list_match_markets',replay_snapshot(manifest,chunks))
        self.assertEqual(original,replayed)

    def test_snapshot_replay_requires_all_exact_chunks_and_hash(self):
        value = {'items': [{'home_team': '长球队名' * 3000}], 'complete': True}
        manifest, blocks = encode_snapshot(value)
        manifest['call_id'] = str(uuid.uuid4())
        chunks = [{'call_id': manifest['call_id'], 'snapshot_sha256': manifest['sha256'],
                   'chunk_index': index, 'data_base64': data} for index, data in enumerate(blocks)]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(base64.b64decode(item['data_base64'])) <= CHUNK_BYTES for item in chunks))
        self.assertEqual(value, replay_snapshot(manifest, list(reversed(chunks))))
        for changed in [chunks[:-1], [chunks[0], chunks[0]],
                        [dict(item, call_id='other') for item in chunks],
                        [dict(item, data_base64=base64.b64encode(b'changed').decode()) for item in chunks]]:
            with self.assertRaises(ValueError):
                replay_snapshot(manifest, changed)

    def test_oversize_never_truncates_and_unknown_fields_are_not_saved(self):
        with self.assertRaises(HTTPException) as caught:
            encode_snapshot({'payload': 'a' * MAX_SNAPSHOT_BYTES})
        self.assertEqual(413, caught.exception.status_code)
        clean = snapshot_result('mcp_resolve_match', {'resolved': True, 'headers': {'authorization': 'secret'},
            'fixture': {'id': 'f', 'home_team': 'Home', 'api_token': 'secret', 'image': 'data:image/png;base64,secret'}})
        self.assertEqual({'resolved': True, 'fixture': {'id': 'f', 'home_team': 'Home'}}, clean)
        with self.assertRaises(HTTPException):
            snapshot_result('mcp_resolve_match', {'resolved': True, 'fixture': {'home_team': 'data:image/png;base64,secret'}})

    def test_signed_cursor_rejects_another_sport_search_key(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(mcp_internal_token='isolated-signing'))))
        review_id, attempt_id = uuid.uuid4(), uuid.uuid4()
        cursor = reviews._cursor(request, review_id, attempt_id, 'a'*64, 1)
        body = reviews.SearchInput(review_id=review_id, attempt_id=attempt_id, team_query='Alpha', sport='football', cursor=cursor)
        self.assertEqual(1, reviews._position(request, body, 'a'*64))
        with self.assertRaises(HTTPException):
            reviews._position(request, body, 'b'*64)


class ToolSportMCPTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = mcp_fixtures.MCPTransportTests.asyncSetUp
    asyncTearDown = mcp_fixtures.MCPTransportTests.asyncTearDown
    rpc = mcp_fixtures.MCPTransportTests.rpc

    async def test_sport_is_optional_schema_and_reaches_real_mcp_business_payload(self):
        captured = []
        def upstream(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={'items': [], 'next_cursor': None})
        await self.server.client.aclose()
        self.server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        tools = (await self.rpc('tools/list')).json()['result']['tools']
        schema = next(tool['inputSchema'] for tool in tools if tool['name'] == 'search_matches')
        self.assertIn('sport', schema['properties'])
        self.assertNotIn('sport', schema.get('required', []))
        response = await self.rpc('tools/call', {'name': 'search_matches', 'arguments': {
            'review_id': 'job', 'attempt_id': 'attempt', 'team_query': 'Alpha', 'sport': 'basketball'}})
        self.assertFalse(response.json()['result'].get('isError', False))
        self.assertEqual('basketball', captured[0]['sport'])


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class ToolEvidenceAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ReviewTests.asyncSetUp
    asyncTearDown = fixtures.ReviewTests.asyncTearDown
    context = fixtures.ReviewTests.context
    post = fixtures.ReviewTests.post
    fixture = fixtures.ReviewTests.fixture
    search_all = fixtures.ReviewTests.search_all
    resolve = fixtures.ReviewTests.resolve
    read_market = fixtures.ReviewTests.read_market

    async def replay(self, kind, *, offset=0):
        records = await self.pool.fetch(
            'SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind=$2 ORDER BY sequence_no OFFSET $3 LIMIT 1',
            self.job, kind, offset)
        payload = records[0]['payload']
        manifest = payload['response_snapshot']
        rows = await self.pool.fetch("SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='tool_response_chunk' "
                                     "AND payload->>'call_id'=$2 ORDER BY sequence_no", self.job, payload['call_id'])
        return payload, replay_snapshot(manifest, [row['payload'] for row in rows])

    async def test_unsupported_coverage_replays_and_fresh_unsupported_quote_cannot_be_recorded(self):
        await self.fixture()
        unsupported_id = uuid.uuid4()
        await self.pool.execute(
            "INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,line,selection,price,"
            "source_updated_at,market_kind,period,status) VALUES($1,'fixture-1','11','11',1,'over',1.95,"
            "now(),'unsupported','source_11','active')", unsupported_id)
        key, _ = await self.search_all()
        self.assertTrue((await self.resolve(key))['resolved'])
        response = await self.post('list_match_markets', fixture_id='fixture-1')
        self.assertEqual(200, response.status_code, response.text)
        wire = response.json()
        self.assertEqual(1, len(wire['markets']))
        self.assertEqual(1, wire['market_coverage']['omitted_unsupported_quotes'])
        receipt, replayed = await self.replay('mcp_list_match_markets')
        self.assertEqual(wire, replayed)
        self.assertEqual([wire['markets'][0]['quote_id']], receipt['response_snapshot']['references']['quote_ids'])
        self.assertEqual(2, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))
        rejected = await self.post('record_expert_signals', signals=[{'fixture_id': 'fixture-1',
            'quote_id': str(unsupported_id), 'evidence_text': 'unsupported cannot become tradable by freshness'}])
        self.assertEqual(200, rejected.status_code, rejected.text)
        self.assertEqual([], rejected.json()['accepted'])
        self.assertEqual('quote_inactive_unknown_or_stale', rejected.json()['rejected'][0]['reason'])
        await self.pool.execute("UPDATE v2_current_quotes SET market_name='changed after snapshot'")
        self.assertEqual(wire, (await self.replay('mcp_list_match_markets'))[1])

    async def test_sport_scope_separates_identical_teams_and_keeps_youth_women(self):
        for index, sport, home, away in [
            (1, 'football', 'Club A', 'Club B'), (2, 'basketball', 'Club A', 'Club B'),
            (3, 'football', 'Club A U21', 'Club B U21'), (4, 'football', 'Club A Women', 'Club B Women'),
            (5, 'source_31', 'Club A', 'Club B')]:
            await self.fixture(index, sport=sport, home_team=home, away_team=away)
        key, items = await self.search_all('Club')
        self.assertEqual(5, len(items))
        ambiguous = await self.post('resolve_match', search_key=key, team_a='Club A', team_b='Club B')
        self.assertFalse(ambiguous.json()['resolved'])
        self.assertEqual(3, ambiguous.json()['candidate_count'])
        result = await self.post('search_matches', team_query='Club', sport=' FOOTBALL ')
        self.assertEqual(200, result.status_code, result.text)
        data = result.json()
        self.assertEqual({'football'}, {row['sport'] for row in data['items']})
        self.assertEqual(3, len(data['items']))
        for home, away, fixture_id in [('Club A', 'Club B', 'fixture-1'),
            ('Club A U21', 'Club B U21', 'fixture-3'), ('Club A Women', 'Club B Women', 'fixture-4')]:
            await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2 WHERE id=$1',
                self.message,home+' VS '+away)
            match = await self.post('resolve_match', search_key=data['search_key'], team_a=home, team_b=away)
            self.assertEqual(fixture_id, match.json()['fixture']['id'])
        unknown = await self.post('search_matches', team_query='Club', sport='source_31')
        self.assertEqual(['fixture-5'], [row['id'] for row in unknown.json()['items']])

    async def test_independent_searches_run_concurrently_with_unique_ordered_evidence(self):
        await self.fixture(1)
        await self.fixture(2, home_team='Gamma2', away_team='Delta2')
        reached=0
        barrier=asyncio.Event()
        original=reviews.active_tool_job
        async def synchronized(conn,review_id,attempt_id):
            nonlocal reached
            result=await original(conn,review_id,attempt_id)
            reached+=1
            if reached<=2:
                if reached==2:barrier.set()
                await asyncio.wait_for(barrier.wait(),timeout=.75)
            return result
        with patch.object(reviews,'active_tool_job',side_effect=synchronized):
            results=await asyncio.gather(
                self.post('search_matches',team_query='Alpha',sport='football'),
                self.post('search_matches',team_query='Gamma',sport='football'))
        self.assertEqual([200,200],[response.status_code for response in results])
        self.assertEqual([['fixture-1'],['fixture-2']],[ [row['id'] for row in response.json()['items']] for response in results])
        traces=await self.pool.fetch("SELECT sequence_no,payload FROM v2_analysis_traces WHERE trace_kind='mcp_search_matches' ORDER BY sequence_no")
        self.assertEqual(2,len(traces))
        self.assertEqual(2,len({row['payload']['call_id'] for row in traces}))
        self.assertEqual(2,len({row['sequence_no'] for row in traces}))
        all_sequences=await self.pool.fetch('SELECT sequence_no FROM v2_analysis_traces WHERE job_id=$1 ORDER BY sequence_no',self.job)
        self.assertEqual(list(range(1,len(all_sequences)+1)),[row['sequence_no'] for row in all_sequences])
        for response in results:
            call_id=(await self.pool.fetchval("SELECT payload->>'call_id' FROM v2_analysis_traces WHERE trace_kind='mcp_search_matches' AND payload->>'search_key'=$1",response.json()['search_key']))
            payload=await self.pool.fetch("SELECT payload FROM v2_analysis_traces WHERE trace_kind='tool_response_chunk' AND payload->>'call_id'=$1 ORDER BY sequence_no",call_id)
            manifest=next(row['payload']['response_snapshot'] for row in traces if row['payload']['call_id']==call_id)
            from app.review_tool_evidence import replay_snapshot
            self.assertEqual(response.json(),replay_snapshot(manifest,[row['payload'] for row in payload]))

    async def test_concurrent_trace_batches_append_contiguous_sequences(self):
        before = await self.pool.fetchval(
            'SELECT COALESCE(MAX(sequence_no),0) FROM v2_analysis_traces WHERE job_id=$1', self.job)

        async def append_batch(batch):
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await append_traces(conn, self.job, [
                        ('trace_batch_test', {'batch': batch, 'part': part}) for part in range(2)
                    ])

        await asyncio.gather(*(append_batch(batch) for batch in range(12)))
        rows = await self.pool.fetch(
            "SELECT sequence_no,payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='trace_batch_test' ORDER BY sequence_no",
            self.job)
        self.assertEqual(list(range(before + 1, before + 25)), [row['sequence_no'] for row in rows])
        self.assertEqual({(batch, part) for batch in range(12) for part in range(2)},
                         {(row['payload']['batch'], row['payload']['part']) for row in rows})

    async def test_same_cursor_requests_serialize_and_stale_parallel_page_is_rejected(self):
        await self.fixture(1)
        await self.fixture(2)
        first=(await self.post('search_matches',team_query='Alpha',sport='football',page_size=1)).json()
        responses=await asyncio.gather(*(
            self.post('search_matches',team_query='Alpha',sport='football',page_size=1,cursor=first['next_cursor'])
            for _ in range(2)))
        self.assertEqual([200,409],sorted(response.status_code for response in responses))
        page=next(response.json() for response in responses if response.status_code==200)
        self.assertEqual(['fixture-2'],[item['id'] for item in page['items']])
        self.assertTrue(page['search_complete'])
        self.assertEqual(3,await self.pool.fetchval("SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='mcp_search_matches'"))

    async def test_independent_market_reads_run_concurrently_without_trace_collision(self):
        await self.fixture(1)
        await self.fixture(2)
        key,_=await self.search_all()
        self.assertTrue((await self.resolve(key,1))['resolved'])
        self.assertTrue((await self.resolve(key,2))['resolved'])
        responses=await asyncio.gather(self.post('list_match_markets',fixture_id='fixture-1'),
                                       self.post('list_match_markets',fixture_id='fixture-2'))
        self.assertEqual([200,200],[response.status_code for response in responses])
        rows=await self.pool.fetch("SELECT sequence_no,payload->>'fixture_id' fixture_id FROM v2_analysis_traces WHERE trace_kind='mcp_list_match_markets' ORDER BY sequence_no")
        self.assertEqual(['fixture-1','fixture-2'],[row['fixture_id'] for row in rows])
        sequences=await self.pool.fetch('SELECT sequence_no FROM v2_analysis_traces WHERE job_id=$1 ORDER BY sequence_no',self.job)
        self.assertEqual(list(range(1,len(sequences)+1)),[row['sequence_no'] for row in sequences])
        self.assertEqual(422, (await self.post('search_matches', team_query='Club', sport='football?token=x')).status_code)

    async def test_independent_fresh_fixture_records_run_concurrently_with_exact_evidence(self):
        await self.fixture(1)
        await self.fixture(2)
        key,_=await self.search_all()
        self.assertTrue((await self.resolve(key,1))['resolved'])
        self.assertTrue((await self.resolve(key,2))['resolved'])
        market1,market2=await asyncio.gather(self.read_market('fixture-1'),self.read_market('fixture-2'))
        reached=0
        barrier=asyncio.Event()
        original=reviews.active_tool_job
        async def synchronized(conn,review_id,attempt_id):
            nonlocal reached
            result=await original(conn,review_id,attempt_id)
            reached+=1
            if reached<=2:
                if reached==2:barrier.set()
                await asyncio.wait_for(barrier.wait(),timeout=.75)
            return result
        picks=[{'fixture_id':'fixture-1','quote_id':market1['quote_id'],'evidence_text':'Alpha1 over 2.5'},
               {'fixture_id':'fixture-2','quote_id':market2['quote_id'],'evidence_text':'Alpha2 over 2.5'}]
        with patch.object(reviews,'active_tool_job',side_effect=synchronized):
            responses=await asyncio.gather(*(self.post('record_expert_signals',signals=[pick]) for pick in picks))
        self.assertEqual([200,200],[response.status_code for response in responses])
        self.assertTrue(all(len(response.json()['accepted'])==1 for response in responses))
        signals=await self.pool.fetch('SELECT fixture_id FROM v2_expert_signals')
        self.assertEqual({'fixture-1','fixture-2'},{row['fixture_id'] for row in signals})
        records=await self.pool.fetch("SELECT sequence_no,payload FROM v2_analysis_traces "
                                      "WHERE trace_kind='mcp_record_expert_signals' ORDER BY sequence_no")
        self.assertEqual({'fixture-1','fixture-2'},
            {row['payload']['accepted'][0]['fixture_id'] for row in records})
        all_sequences=await self.pool.fetch('SELECT sequence_no FROM v2_analysis_traces WHERE job_id=$1 ORDER BY sequence_no',self.job)
        self.assertEqual(list(range(1,len(all_sequences)+1)),[row['sequence_no'] for row in all_sequences])
        for response,fixture_id in zip(responses,('fixture-1','fixture-2')):
            call_id=next(row['payload']['call_id'] for row in records
                         if row['payload']['accepted'][0]['fixture_id']==fixture_id)
            rows=await self.pool.fetch("SELECT payload FROM v2_analysis_traces WHERE trace_kind='tool_response_chunk' "
                                       "AND payload->>'call_id'=$1 ORDER BY sequence_no",call_id)
            manifest=next(row['payload']['response_snapshot'] for row in records
                          if row['payload']['call_id']==call_id)
            self.assertEqual(response.json(),replay_snapshot(manifest,[row['payload'] for row in rows]))

    async def test_signed_cursor_is_bound_to_sport_and_failed_scope_does_not_advance(self):
        await self.fixture(1)
        await self.fixture(2)
        first = (await self.post('search_matches', team_query='Alpha', sport='football', page_size=1)).json()
        for sport in ('basketball', 'tennis'):
            rejected = await self.post('search_matches', team_query='Alpha', sport=sport, page_size=1, cursor=first['next_cursor'])
            self.assertEqual(409, rejected.status_code)
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_match_searches'))
        # Omitted/null sport continues the authenticated football snapshot;
        # it must not become an unfiltered search.
        second = await self.post('search_matches', team_query='Alpha', sport=None, page_size=1, cursor=first['next_cursor'])
        self.assertEqual(200, second.status_code, second.text)
        self.assertTrue(second.json()['search_complete'])
        failures = await self.pool.fetch("SELECT payload FROM v2_analysis_traces WHERE payload->>'status'='failed'")
        self.assertEqual(2, len(failures))
        self.assertTrue(all(row['payload']['elapsed_ms'] >= 0 for row in failures))

    async def test_cross_day_all_states_and_every_page_replays_exact_response(self):
        midnight = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        await self.fixture(1, phase='live', kickoff_at=(midnight - timedelta(minutes=30)).isoformat())
        await self.fixture(2, phase='scheduled', kickoff_at=(midnight + timedelta(minutes=30)).isoformat())
        await self.fixture(3, phase='finished', kickoff_at=(midnight - timedelta(hours=1)).isoformat())
        args = {'team_query': 'Alpha', 'sport': 'football', 'from_utc': (midnight-timedelta(hours=1)).isoformat(),
                'to_utc': (midnight+timedelta(hours=1)).isoformat(), 'page_size': 1}
        first = (await self.post('search_matches', **args)).json()
        await self.pool.execute("UPDATE v2_fixtures SET home_team='Changed later' WHERE id='fixture-1'")
        second = (await self.post('search_matches', **args, cursor=first['next_cursor'])).json()
        third = (await self.post('search_matches', **args, cursor=second['next_cursor'])).json()
        pages = [first, second, third]
        self.assertEqual(['fixture-3', 'fixture-1', 'fixture-2'], [page['items'][0]['id'] for page in pages])
        self.assertEqual({'finished', 'live', 'scheduled'}, {page['items'][0]['phase'] for page in pages})
        self.assertEqual('Alpha1', second['items'][0]['home_team'])
        self.assertTrue(third['search_complete'])
        self.assertIsNone(third['next_cursor'])
        self.assertEqual([3, 3, 3], [page['total_candidates'] for page in (first, second, third)])
        for index, expected in enumerate(pages):
            payload, replayed = await self.replay('mcp_search_matches', offset=index)
            self.assertEqual(expected, replayed)
            self.assertEqual('football', payload['scope']['sport'])
            self.assertEqual(1, payload['message_revision'])
            self.assertEqual(str(self.message), payload['message_id'])
        self.assertEqual(3, third['total_candidates'])

    async def test_search_uses_bounded_database_round_trips_for_first_and_next_page(self):
        await self.fixture(1)
        await self.fixture(2)
        counted_pool = _CountingPool(self.pool)
        with patch('app.routes.reviews.database.current_pool', return_value=counted_pool):
            first = await self.post('search_matches', team_query='Alpha', sport='football', page_size=1)
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(8, counted_pool.commands)
        first_result = first.json()
        self.assertEqual(2, first_result['total_candidates'])

        counted_pool.commands = 0
        with patch('app.routes.reviews.database.current_pool', return_value=counted_pool):
            second = await self.post('search_matches', team_query='Alpha', sport='football', page_size=1,
                                     cursor=first_result['next_cursor'])
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual(6, counted_pool.commands)
        self.assertEqual(2, second.json()['total_candidates'])

    async def test_complete_first_page_reuses_all_candidates_for_inline_binding(self):
        await self.fixture(1)
        counted_pool = _CountingPool(self.pool)
        with patch('app.routes.reviews.database.current_pool', return_value=counted_pool):
            response = await self.post('search_matches', team_query='Alpha', sport='football',
                                       team_a='Alpha1', team_b='Beta1')
        self.assertEqual(200,response.status_code,response.text)
        # Includes one source-identity evidence read, reusing the candidate page.
        self.assertEqual(10,counted_pool.commands)
        wire=response.json()
        self.assertTrue(wire['resolution']['resolved'])
        self.assertEqual('fixture-1',wire['resolution']['fixture']['id'])
        self.assertEqual('scheduled',wire['resolution']['fixture']['phase'])
        self.assertEqual(wire,(await self.replay('mcp_search_matches'))[1])
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))

    async def test_final_page_inline_binding_includes_a_previous_page_candidate(self):
        await self.fixture(1)
        await self.fixture(2)
        scope=dict(team_query='Alpha',sport='football',page_size=1,team_a='Alpha1',team_b='Beta1')
        first=(await self.post('search_matches',**scope)).json()
        self.assertFalse(first['search_complete'])
        self.assertFalse(first['resolution']['resolved'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        last=await self.post('search_matches',**scope,cursor=first['next_cursor'])
        self.assertEqual(200,last.status_code,last.text)
        wire=last.json()
        self.assertEqual(['fixture-2'],[r['id'] for r in wire['items']])
        self.assertTrue(wire['resolution']['resolved'])
        self.assertEqual('fixture-1',wire['resolution']['fixture']['id'])
        self.assertEqual(wire,(await self.replay('mcp_search_matches',offset=1))[1])

    async def test_market_snapshot_is_exact_multi_chunk_and_immutable_after_price_change(self):
        await self.fixture()
        await self.pool.execute("ALTER TABLE v2_fixtures ADD COLUMN private_token text DEFAULT 'must-not-store'")
        await self.pool.execute("ALTER TABLE v2_current_quotes ADD COLUMN private_headers jsonb DEFAULT '{\"authorization\":\"must-not-store\"}'")
        key, _ = await self.search_all()
        stored = await self.pool.fetchval('SELECT candidate_data FROM v2_match_search_candidates LIMIT 1')
        self.assertNotIn('private_token', stored)
        await self.resolve(key)
        request_sequence = await self.pool.fetchval("INSERT INTO v2_analysis_traces(id,job_id,sequence_no,trace_kind,payload) "
            "SELECT $1,$2,max(sequence_no)+1,'hermes_request',$3 FROM v2_analysis_traces WHERE job_id=$2 RETURNING sequence_no", uuid.uuid4(), self.job,
            {'attempt_id': str(self.attempt), 'model': 'test-route/model', 'configuration_version': 'a'*64})
        for index in range(60):
            await self.pool.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,line,selection,price,source_updated_at,market_kind,period) "
                "VALUES($1,'fixture-1',$2,$3,2.5,'over',1.95,now(),'total','full_time')", uuid.uuid4(), f'total-{index}', '盘口' * 70)
        response = await self.post('list_match_markets', fixture_id='fixture-1')
        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(61, len(data['markets']))
        audit = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='mcp_list_match_markets' AND payload->>'status'='succeeded'")
        self.assertEqual(request_sequence, audit['hermes_request_sequence'])
        self.assertEqual('a'*64, audit['configuration_version'])
        self.assertEqual('test-route/model', audit['model'])
        self.assertTrue(all(set(item)==review_tool_evidence.MODEL_QUOTE_FIELDS for item in data['markets']))
        self.assertNotIn('private_headers', data['markets'][0])
        payload, replayed = await self.replay('mcp_list_match_markets')
        self.assertEqual(data, replayed)
        self.assertEqual(['fixture-1'], payload['response_snapshot']['references']['fixture_ids'])
        self.assertEqual(sorted(item['quote_id'] for item in data['markets']), payload['response_snapshot']['references']['quote_ids'])
        self.assertGreater(payload['response_snapshot']['chunk_count'], 1)
        self.assertEqual('test-route/model', payload['model'])
        self.assertEqual('a'*64, payload['configuration_version'])
        before = await self.pool.fetch("SELECT sequence_no,payload FROM v2_analysis_traces ORDER BY sequence_no")
        await self.pool.execute("UPDATE v2_current_quotes SET price=2.5,status='suspended'")
        self.assertEqual(data, (await self.replay('mcp_list_match_markets'))[1])
        self.assertEqual(before, await self.pool.fetch("SELECT sequence_no,payload FROM v2_analysis_traces ORDER BY sequence_no"))
        encoded = json.dumps([row['payload'] for row in before])
        self.assertNotIn('must-not-store', encoded)

    async def test_oversize_rolls_back_reads_and_search_then_records_safe_failure(self):
        await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        with patch('app.review_tool_evidence.MAX_SNAPSHOT_BYTES', 100):
            response = await self.post('list_match_markets', fixture_id='fixture-1')
        self.assertEqual(413, response.status_code)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='mcp_list_match_markets' AND payload->>'status'='succeeded'"))
        failure = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='mcp_list_match_markets'")
        self.assertEqual('evidence_budget_exceeded', failure['failure_category'])
        self.assertNotIn('response_snapshot', failure)
        with patch('app.review_tool_evidence.MAX_SNAPSHOT_BYTES', 100):
            search = await self.post('search_matches', team_query='Alpha', sport='football')
        self.assertEqual(413, search.status_code)
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_match_searches'))

    async def test_resolution_and_record_responses_replay_all_seen_fields(self):
        await self.fixture()
        key, _ = await self.search_all()
        resolved = (await self.post('resolve_match', search_key=key, team_a='Alpha1', team_b='Beta1')).json()
        self.assertEqual(resolved, (await self.replay('mcp_resolve_match'))[1])
        markets = (await self.post('list_match_markets', fixture_id='fixture-1')).json()
        result = await self.post('record_expert_signals', signals=[{'fixture_id': 'fixture-1',
            'quote_id': markets['markets'][0]['quote_id'], 'evidence_text': 'Bearer raw-message-credential-do-not-save 大2.5'}])
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual(1, len(result.json()['accepted']))
        payload, replayed = await self.replay('mcp_record_expert_signals')
        self.assertEqual(result.json(), replayed)
        self.assertNotIn('raw-message-credential-do-not-save', json.dumps(payload))
        self.assertEqual(64, len(payload['parameters']['signals'][0]['evidence_text_sha256']))

    async def test_over_market_count_has_no_partial_reads_and_revoked_attempt_no_failure_trace(self):
        await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        with patch.object(reviews, 'MAX_MARKETS', 0):
            response = await self.post('list_match_markets', fixture_id='fixture-1')
        self.assertEqual(413, response.status_code)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))
        before = await self.pool.fetchval('SELECT count(*) FROM v2_analysis_traces')
        original_active = review_tool_evidence.active_job
        calls = 0
        async def expire_after_initial_check(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return await original_active(*args, **kwargs)
            raise HTTPException(409, 'explicit expired audit attempt')
        with patch('app.review_tool_evidence.active_job', side_effect=expire_after_initial_check):
            response = await self.post('list_match_markets', fixture_id='not-bound')
        self.assertEqual(409, response.status_code)
        self.assertEqual('fixture must be uniquely resolved first', response.json()['detail'])
        self.assertEqual(before, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_traces'))

    async def test_unauthorized_expired_wrong_attempt_cannot_append_traces(self):
        await self.fixture()
        invalid = self.context(team_query='Alpha')
        for payload, headers in [(invalid, {'Authorization': 'Bearer wrong'}),
                                 (dict(invalid, attempt_id=str(uuid.uuid4())), {})]:
            self.assertIn((await self.client.post('/internal/mcp/search_matches', json=payload, headers=headers)).status_code, (401,409))
        await self.pool.execute("UPDATE v2_analysis_jobs SET lease_until=now()-interval '1 second'")
        self.assertEqual(409, (await self.post('resolve_match', search_key='a'*64, team_a='Alpha1', team_b='Beta1')).status_code)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_traces'))

    async def test_failure_audit_cannot_mask_original_http_or_database_failure(self):
        with patch('app.review_tool_evidence.trace', AsyncMock(side_effect=RuntimeError('secret must not escape'))):
            response = await self.post('list_match_markets', fixture_id='not-bound')
        self.assertEqual(409, response.status_code)
        self.assertEqual('fixture must be uniquely resolved first', response.json()['detail'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_traces'))
        with patch.object(reviews, 'active_tool_job', AsyncMock(side_effect=ConnectionError('explicit isolated DB failure'))):
            with self.assertRaises(ConnectionError):
                await self.post('search_matches', team_query='Alpha')
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_traces'))
