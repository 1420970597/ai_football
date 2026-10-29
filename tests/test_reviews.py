import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import uuid

import asyncpg
from fastapi import FastAPI
import httpx

from app import database
from app.database import apply_migrations
from app.ingestion import ingest_fixture, ingest_fixture_batch
from app.routes import internal, reviews


@unittest.skipUnless(os.environ.get("TEST_DATABASE_ADMIN_URL"), "requires isolated database")
class ReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.admin_url = os.environ["TEST_DATABASE_ADMIN_URL"]
        self.dbname = "test_reviews_" + uuid.uuid4().hex
        self.admin = await asyncpg.connect(self.admin_url)
        await self.admin.execute(f'CREATE DATABASE "{self.dbname}"')
        url = self.admin_url.rsplit("/", 1)[0] + "/" + self.dbname

        async def codecs(conn):
            await conn.set_type_codec("jsonb", schema="pg_catalog", encoder=json.dumps, decoder=json.loads)

        self.pool = await asyncpg.create_pool(url, min_size=1, max_size=4, init=codecs)
        async with self.pool.acquire() as conn:
            await apply_migrations(conn)
        database.pool = self.pool
        app = FastAPI()
        app.state.settings = SimpleNamespace(mcp_internal_token="isolated-tools", collector_token="isolated-collector", quote_max_age_seconds=60)
        app.include_router(reviews.router)
        app.include_router(internal.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer isolated-tools"})
        self.now = datetime.now(timezone.utc)
        self.source, self.expert, self.message, self.job, self.attempt = [uuid.uuid4() for _ in range(5)]
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'chat','Source')", self.source)
        await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'author','专家昵称')", self.expert)
        await self.pool.execute("INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)", self.source, self.expert)
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,'100','Alpha Beta over',now())", self.message, self.source, self.expert)
        await self.pool.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,attempt_id,lease_until) VALUES($1,$2,'running',$3,$4,now()+interval '5 minute')", self.job, self.message, str(self.job), self.attempt)

    async def asyncTearDown(self):
        await self.client.aclose()
        database.pool = None
        await self.pool.close()
        await self.admin.execute(f'DROP DATABASE "{self.dbname}" WITH (FORCE)')
        await self.admin.close()

    def context(self, **extra):
        return {"review_id": str(self.job), "attempt_id": str(self.attempt), **extra}

    async def post(self, tool, **extra):
        response = await self.client.post("/internal/mcp/" + tool, json=self.context(**extra))
        return response

    async def fixture(self, index=1, **updates):
        fixture_id = f"fixture-{index}"
        payload = {"id": fixture_id, "source_id": fixture_id, "sport": "football", "competition_name": "League", "home_team": f"Alpha{index}", "away_team": f"Beta{index}",
                   "kickoff_at": (self.now + timedelta(hours=index)).isoformat(), "phase": "scheduled", "source_updated_at": self.now.isoformat(), "received_at": self.now.isoformat(),
                   "markets": [{"market_id": "total", "market_name": "全场大小", "kind": "total", "period": "full_time", "line": "2.5", "selection": "over", "price": "1.95", "source_updated_at": self.now.isoformat(), "received_at": self.now.isoformat()}], **updates}
        body = internal.FixtureInput.model_validate(payload)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await ingest_fixture(conn, body)
        return fixture_id

    async def search_all(self, team="Alpha", page_size=20, phase="all"):
        pages = []
        cursor = None
        while True:
            response = await self.post("search_matches", team_query=team, cursor=cursor, page_size=page_size, phase=phase)
            self.assertEqual(200, response.status_code, response.text)
            result = response.json()
            pages.extend(result["items"])
            cursor = result["next_cursor"]
            if not cursor:
                return result["search_key"], pages

    async def resolve(self, key, index=1):
        result = await self.post("resolve_match", search_key=key, team_a=f"Alpha{index}", team_b=f"Beta{index}")
        self.assertEqual(200, result.status_code, result.text)
        return result.json()

    async def read_market(self, fixture_id):
        result = await self.post("list_match_markets", fixture_id=fixture_id)
        self.assertEqual(200, result.status_code, result.text)
        return result.json()["markets"][0]

    async def test_all_states_are_identifiable_but_inactive_matches_cannot_read_or_record(self):
        for index, phase in enumerate(('scheduled', 'live', 'finished', 'cancelled'), 1):
            await self.fixture(index, phase=phase)
        key, candidates = await self.search_all(page_size=1)
        self.assertEqual({'scheduled', 'live', 'finished', 'cancelled'}, {item['phase'] for item in candidates})
        for index, phase in enumerate(('scheduled', 'live', 'finished', 'cancelled'), 1):
            resolved = await self.resolve(key, index)
            self.assertTrue(resolved['resolved'])
            self.assertEqual(phase, resolved['fixture']['phase'])
            self.assertEqual(index < 3, resolved['fixture']['realtime_eligible'])
            if index < 3:
                continue
            self.assertEqual(phase, resolved['fixture']['eligibility_reason'])
            markets = await self.post('list_match_markets', fixture_id=f'fixture-{index}')
            self.assertEqual(409, markets.status_code)
            self.assertEqual('fixture is no longer active', markets.json()['detail'])
            quote_id = await self.pool.fetchval('SELECT id FROM v2_current_quotes WHERE fixture_id=$1', f'fixture-{index}')
            recorded = await self.post('record_expert_signals', signals=[{
                'fixture_id': f'fixture-{index}', 'quote_id': str(quote_id), 'evidence_text': '完场取消不得录入'}])
            self.assertEqual([], recorded.json()['accepted'])
            self.assertEqual('quote_not_read_or_changed', recorded.json()['rejected'][0]['reason'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_each_explicit_phase_filters_candidates(self):
        for index, phase in enumerate(('scheduled', 'live', 'finished', 'cancelled'), 1):
            await self.fixture(index, phase=phase)
        for phase in ('scheduled', 'live', 'finished', 'cancelled'):
            _, candidates = await self.search_all(phase=phase)
            self.assertEqual([phase], [item['phase'] for item in candidates])

    async def test_resolution_reports_current_phase_when_fixture_finishes_after_search(self):
        fixture = await self.fixture()
        key, _ = await self.search_all()
        await self.pool.execute("UPDATE v2_fixtures SET phase='finished' WHERE id=$1", fixture)
        result = await self.resolve(key)
        self.assertTrue(result['resolved'])
        self.assertEqual('finished', result['fixture']['phase'])
        self.assertFalse(result['fixture']['realtime_eligible'])
        response = await self.post('list_match_markets', fixture_id=fixture)
        self.assertEqual(409, response.status_code)

    async def test_no_matching_and_ambiguous_fixture_have_distinct_reasons(self):
        await self.fixture(1)
        await self.fixture(2, home_team='Alpha1', away_team='Beta1', phase='finished')
        key, _ = await self.search_all()
        ambiguous = await self.resolve(key)
        self.assertFalse(ambiguous['resolved'])
        self.assertEqual('ambiguous_fixture', ambiguous['reason'])
        self.assertEqual(2, ambiguous['candidate_count'])
        missing = await self.resolve(key, 3)
        self.assertEqual('no_matching_fixture', missing['reason'])
        self.assertEqual(0, missing['candidate_count'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))

    async def test_record_waits_for_fixture_before_locking_quote_during_ingestion(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        quote = await self.read_market(fixture_id)
        pick = {'fixture_id': fixture_id, 'quote_id': quote['quote_id'], 'evidence_text': 'Alpha over'}
        task = None
        async with self.pool.acquire() as collector:
            transaction = collector.transaction()
            await transaction.start()
            try:
                await collector.execute('SELECT id FROM v2_fixtures WHERE id=$1 FOR UPDATE', fixture_id)
                collector_pid = await collector.fetchval('SELECT pg_backend_pid()')
                task = asyncio.create_task(self.post('record_expert_signals', signals=[pick]))
                async with asyncio.timeout(5):
                    while not await self.pool.fetchval(
                        'SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE $1=ANY(pg_blocking_pids(pid)))', collector_pid):
                        await asyncio.sleep(.01)
                # Projection owns the fixture first. It must be able to update
                # its quote while the reviewer waits, without a lock cycle.
                await collector.execute("SET LOCAL lock_timeout='300ms'")
                await collector.execute('UPDATE v2_current_quotes SET price=2.05 WHERE id=$1', uuid.UUID(quote['quote_id']))
                await transaction.commit()
            except BaseException:
                await transaction.rollback()
                raise
            finally:
                if task is not None:
                    response = await asyncio.wait_for(task, 5)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([], response.json()['accepted'])
        self.assertEqual('quote_not_read_or_changed', response.json()['rejected'][0]['reason'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        await self.read_market(fixture_id)
        retry = await self.post('record_expert_signals', signals=[pick])
        self.assertEqual(1, len(retry.json()['accepted']))
        self.assertEqual('2.05000', retry.json()['accepted'][0]['price'])

    async def test_bound_stale_market_refresh_rechecks_source_timestamp_and_preserves_gate(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '5 minutes'")
        settings = self.client._transport.app.state.settings
        settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        calls = []
        factory = httpx.AsyncClient
        async def handler(request):
            calls.append(json.loads(request.content))
            self.assertEqual(self.pool.get_size(), self.pool.get_idle_size())
            self.assertEqual('Bearer isolated-collector', request.headers['authorization'])
            return httpx.Response(200, json={'accepted': True, 'reason': 'refresh_completed'})
        with patch.object(reviews.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            response = await self.post('list_match_markets', fixture_id=fixture_id)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([{'fixture_id': fixture_id, 'source_match_id': fixture_id}], calls)
        self.assertTrue(response.json()['refresh']['accepted'])
        self.assertTrue(all(not row['fresh'] for row in response.json()['markets']))
        quote = response.json()['markets'][0]
        record = await self.post('record_expert_signals', signals=[{'fixture_id': fixture_id,
            'quote_id': quote['quote_id'], 'evidence_text': '大2.5'}])
        self.assertEqual('quote_inactive_unknown_or_stale', record.json()['rejected'][0]['reason'])

    async def test_bound_refresh_can_make_current_scheduled_quote_usable(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '5 minutes'")
        self.client._transport.app.state.settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        factory = httpx.AsyncClient
        async def handler(request):
            await self.pool.execute('UPDATE v2_current_quotes SET source_updated_at=now()')
            return httpx.Response(200, json={'accepted': True, 'reason': 'written'})
        with patch.object(reviews.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            response = await self.post('list_match_markets', fixture_id=fixture_id)
        self.assertEqual(200, response.status_code, response.text)
        quote = response.json()['markets'][0]
        self.assertTrue(quote['fresh'])
        result = await self.post('record_expert_signals', signals=[{'fixture_id': fixture_id,
            'quote_id': quote['quote_id'], 'evidence_text': '赛前 大2.5'}])
        self.assertEqual(1, len(result.json()['accepted']))

    async def test_lease_is_rechecked_after_refresh_and_timeout_does_not_make_price_fresh(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '5 minutes'")
        self.client._transport.app.state.settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        factory = httpx.AsyncClient
        async def handler(request):
            raise httpx.ReadTimeout('isolated timeout', request=request)
        with patch.object(reviews.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            response = await self.post('list_match_markets', fixture_id=fixture_id)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('refresh_unavailable', response.json()['refresh']['reason'])
        self.assertTrue(all(not item['fresh'] for item in response.json()['markets']))
        async def expire(request):
            await self.pool.execute("UPDATE v2_analysis_jobs SET lease_until=now()-interval '1 second'")
            return httpx.Response(200, json={'accepted': True, 'reason': 'written'})
        with patch.object(reviews.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(expire), **kw)):
            expired = await self.post('list_match_markets', fixture_id=fixture_id)
        self.assertEqual(409, expired.status_code, expired.text)

    async def test_unbound_or_historical_review_cannot_trigger_source_refresh(self):
        fixture_id = await self.fixture()
        self.client._transport.app.state.settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        factory = httpx.AsyncClient
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={'accepted': True})
        with patch.object(reviews.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            unbound = await self.post('list_match_markets', fixture_id=fixture_id)
            self.assertEqual(409, unbound.status_code)
            key, _ = await self.search_all()
            await self.resolve(key)
            await self.pool.execute("UPDATE v2_analysis_jobs SET review_mode='historical_replay'")
            historical = await self.post('list_match_markets', fixture_id=fixture_id)
        self.assertEqual(200, historical.status_code, historical.text)
        self.assertEqual('historical_review_not_current', historical.json()['refresh']['reason'])
        self.assertEqual([], requests)

    async def test_current_detail_confirmation_records_unchanged_quote_without_rewriting_source_time(self):
        from app.quote_observation import DETAIL_PATH
        fixture_id = await self.fixture(markets=[{'market_id':'4','market_name':'全场让球','kind':'handicap',
            'period':'full_time','line':'-0.5','selection':'home','price':'1.95','status':'active',
            'source_updated_at':(self.now-timedelta(hours=1)).isoformat(),'received_at':self.now.isoformat(),
            'play_id':'4','market_instance_id':'native-market','selection_id':'native-option'}])
        key, _ = await self.search_all()
        await self.resolve(key)
        settings = self.client._transport.app.state.settings
        settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        settings.quote_max_age_seconds = 15
        factory = httpx.AsyncClient
        observations = []
        async def handler(request):
            q = dict(await self.pool.fetchrow('SELECT * FROM v2_current_quotes WHERE fixture_id=$1', fixture_id))
            f = dict(await self.pool.fetchrow('SELECT * FROM v2_fixtures WHERE id=$1', fixture_id))
            from app.quote_observation import CONTEXT_FIELDS
            now = datetime.now(timezone.utc).isoformat()
            observation = {'path':DETAIL_PATH,'started_at':now,'completed_at':now,'fixture_id':fixture_id,
                'source_match_id':fixture_id,'context':{k:f[k] for k in CONTEXT_FIELDS},
                'quotes':[json.loads(json.dumps(q, default=str))]}
            observations.append(observation)
            return httpx.Response(200,json={'accepted':False,'reason':'parent_unchanged_recheck_quotes',
                'snapshot_committed':True,'current_observation':observation})
        with patch.object(reviews.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            quote = await self.read_market(fixture_id)
            self.assertFalse(quote['fresh'])
            self.assertTrue(quote['confirmed_current'])
            self.assertTrue(quote['can_record'])
            recorded = await self.post('record_expert_signals', signals=[{'fixture_id':fixture_id,
                'quote_id':quote['quote_id'],'evidence_text':'Alpha1 -0.5'}])
        self.assertEqual(1, len(recorded.json()['accepted']), recorded.text)
        self.assertEqual(1, len(observations), 'recent exact observation should not duplicate provider I/O')
        signal = await self.pool.fetchrow('SELECT * FROM v2_expert_signals')
        self.assertEqual(self.now-timedelta(hours=1), signal['quote_time'])
        evidence = await self.pool.fetchval('SELECT evidence FROM v2_quote_evidence')
        self.assertEqual(DETAIL_PATH, evidence['current_confirmation']['path'])
        # An observation is not an execution permit or a fabricated new ctsp.
        from app.review_state import fresh_quote
        stored = await self.pool.fetchrow('SELECT * FROM v2_current_quotes')
        self.assertFalse(fresh_quote(stored,15))

    async def test_record_rechecks_expired_observation_and_rejects_changed_price(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        quote = await self.read_market(fixture_id)
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '5 minutes'")
        settings = self.client._transport.app.state.settings
        settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        factory = httpx.AsyncClient
        calls=[]
        async def handler(request):
            calls.append(request)
            self.assertEqual(self.pool.get_size(),self.pool.get_idle_size())
            await self.pool.execute('UPDATE v2_current_quotes SET price=2.01,source_updated_at=now()')
            return httpx.Response(200,json={'accepted':True,'reason':'written'})
        with patch.object(reviews.httpx,'AsyncClient',side_effect=lambda **kw:factory(transport=httpx.MockTransport(handler),**kw)):
            result = await self.post('record_expert_signals',signals=[{'fixture_id':fixture_id,
                'quote_id':quote['quote_id'],'evidence_text':'大2.5'}])
        self.assertEqual(1,len(calls))
        self.assertEqual('quote_not_read_or_changed',result.json()['rejected'][0]['reason'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_search_reaches_105th_fixture_and_snapshot_is_stable(self):
        for index in range(105):
            await self.fixture(index)
        first = await self.post("search_matches", team_query="Alpha", page_size=20)
        self.assertEqual(200, first.status_code, first.text)
        result = first.json()
        await self.fixture(999)
        count = len(result["items"])
        while result["next_cursor"]:
            page = await self.post("search_matches", team_query="Alpha", page_size=20, cursor=result["next_cursor"])
            self.assertEqual(200, page.status_code, page.text)
            result = page.json()
            count += len(result["items"])
        self.assertEqual(105, count)
        resolved = await self.resolve(result["search_key"], 104)
        self.assertTrue(resolved["resolved"])
        self.assertEqual("fixture-104", resolved["fixture"]["id"])

    async def test_default_search_page_returns_100_and_cursor_completes_remaining_candidates(self):
        for index in range(105):
            await self.fixture(index)
        first = await self.post("search_matches", team_query="Alpha")
        self.assertEqual(200, first.status_code, first.text)
        result = first.json()
        self.assertEqual(100, len(result["items"]))
        self.assertTrue(result["next_cursor"])
        second = await self.post("search_matches", team_query="Alpha", cursor=result["next_cursor"])
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual(5, len(second.json()["items"]))
        self.assertIsNone(second.json()["next_cursor"])
        self.assertTrue(second.json()["search_complete"])

    async def test_resolution_before_full_pagination_is_rejected(self):
        await self.fixture(1)
        await self.fixture(2)
        result = (await self.post("search_matches", team_query="Alpha", page_size=1)).json()
        response = await self.post("resolve_match", search_key=result["search_key"], team_a="Alpha1", team_b="Beta1")
        self.assertEqual(409, response.status_code)
        direct = await self.post("list_match_markets", fixture_id="fixture-1")
        self.assertEqual(409, direct.status_code)

    async def test_two_matches_with_same_teams_require_time_disambiguation(self):
        await self.fixture(1, home_team="Club A", away_team="Club B")
        await self.fixture(2, home_team="Club A", away_team="Club B")
        key, _ = await self.search_all("Club")
        result = await self.post("resolve_match", search_key=key, team_a="Club A", team_b="Club B")
        self.assertFalse(result.json()["resolved"])
        self.assertEqual(2, result.json()["candidate_count"])
        result = await self.post("resolve_match", search_key=key, team_a="Club A", team_b="Club B", to_utc=(self.now + timedelta(hours=1, minutes=30)).isoformat())
        self.assertTrue(result.json()["resolved"])

    async def test_signal_requires_market_read_and_unchanged_quote(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        quote_id = await self.pool.fetchval("SELECT id FROM v2_current_quotes LIMIT 1")
        pick = {"fixture_id": fixture_id, "quote_id": str(quote_id), "evidence_text": "Alpha1 vs Beta1 大2.5"}
        result = await self.post("record_expert_signals", signals=[pick])
        self.assertEqual("quote_not_read_or_changed", result.json()["rejected"][0]["reason"])
        await self.read_market(fixture_id)
        await self.pool.execute("UPDATE v2_current_quotes SET price=2.05 WHERE id=$1", quote_id)
        result = await self.post("record_expert_signals", signals=[pick])
        self.assertFalse(result.json()["accepted"])
        await self.read_market(fixture_id)
        result = await self.post("record_expert_signals", signals=[pick])
        self.assertEqual(Decimal('2.05'), Decimal(result.json()["accepted"][0]["price"]))
        duplicate = await self.post("record_expert_signals", signals=[pick])
        self.assertTrue(duplicate.json()["accepted"][0]["duplicate"])
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_quote_evidence"))
        traces = await self.pool.fetch("SELECT payload FROM v2_analysis_traces WHERE trace_kind LIKE 'mcp_%'")
        self.assertTrue(traces)
        self.assertTrue(all(row['payload']['elapsed_ms'] >= 0 for row in traces))

    async def test_same_market_for_two_fixtures_creates_two_signals(self):
        await self.fixture(1)
        await self.fixture(2, phase="live", home_score=1, away_score=0)
        key, _ = await self.search_all()
        picks = []
        for index in (1, 2):
            await self.resolve(key, index)
            quote = await self.read_market(f"fixture-{index}")
            picks.append({"fixture_id": f"fixture-{index}", "quote_id": quote["quote_id"], "evidence_text": f"Alpha{index} Beta{index} 大2.5"})
        result = await self.post("record_expert_signals", signals=picks)
        self.assertEqual(2, len(result.json()["accepted"]))

    async def test_historical_replay_cannot_bind_old_recommendation_to_current_price(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        quote = await self.read_market(fixture_id)
        await self.pool.execute("UPDATE v2_analysis_jobs SET review_mode='historical_replay' WHERE id=$1", self.job)
        response = await self.post('record_expert_signals', signals=[{
            'fixture_id':fixture_id, 'quote_id':quote['quote_id'], 'evidence_text':'历史推荐'}])
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual([],response.json()['accepted'])
        self.assertEqual('historical_review_not_current',response.json()['rejected'][0]['reason'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))
        trace = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='mcp_record_expert_signals'")
        self.assertEqual('historical_replay',trace['review_mode'])

    async def _reset_job_posted_at(self, posted_at):
        await self.pool.execute('DELETE FROM v2_analysis_jobs WHERE id=$1', self.job)
        await self.pool.execute('UPDATE v2_expert_messages SET posted_at=$2 WHERE id=$1', self.message, posted_at)
        await self.pool.execute(
            "INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,attempt_id,lease_until) "
            "VALUES($1,$2,'running',$3,$4,now()+interval '5 minute')",
            self.job, self.message, str(self.job), self.attempt)

    async def test_realtime_mode_cannot_bypass_original_posting_age(self):
        await self._reset_job_posted_at(self.now - timedelta(days=2))
        response = await self.post('record_expert_signals', signals=[{
            'fixture_id': 'fixture-1', 'quote_id': str(uuid.uuid4()), 'evidence_text': '旧消息'}])
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([], response.json()['accepted'])
        self.assertEqual('historical_review_not_current', response.json()['rejected'][0]['reason'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_future_posting_date_cannot_create_current_recommendation(self):
        await self._reset_job_posted_at(self.now + timedelta(hours=1))
        response = await self.post('record_expert_signals', signals=[{
            'fixture_id': 'fixture-1', 'quote_id': str(uuid.uuid4()), 'evidence_text': '未来消息'}])
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([], response.json()['accepted'])
        self.assertEqual('message_timestamp_in_future', response.json()['rejected'][0]['reason'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))

    async def test_cursor_tamper_scope_change_and_page_replay_rejected(self):
        await self.fixture(1)
        await self.fixture(2)
        first = (await self.post("search_matches", team_query="Alpha", page_size=1)).json()
        cursor = first["next_cursor"]
        bad = cursor[:-4] + "AAAA"
        self.assertNotEqual(200, (await self.post("search_matches", team_query="Alpha", cursor=bad, page_size=1)).status_code)
        self.assertEqual(409, (await self.post("search_matches", team_query="Beta", cursor=cursor, page_size=1)).status_code)
        self.assertEqual(409, (await self.post("search_matches", team_query="Alpha", page_size=1)).status_code)
        self.assertEqual(200, (await self.post("search_matches", team_query="Alpha", cursor=cursor, page_size=1)).status_code)

    async def test_old_attempt_source_disabled_and_message_edit_rejected(self):
        await self.fixture()
        await self.pool.execute("UPDATE v2_analysis_jobs SET attempt_id=$1 WHERE id=$2", uuid.uuid4(), self.job)
        self.assertEqual(409, (await self.post("search_matches", team_query="Alpha")).status_code)
        await self.pool.execute("UPDATE v2_analysis_jobs SET attempt_id=$1 WHERE id=$2", self.attempt, self.job)
        await self.pool.execute("UPDATE v2_expert_sources SET enabled=false")
        self.assertEqual(409, (await self.post("search_matches", team_query="Alpha")).status_code)
        await self.pool.execute("UPDATE v2_expert_sources SET enabled=true")
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at,revision) VALUES($1,$2,$3,'100','edited',now(),2)", uuid.uuid4(), self.source, self.expert)
        self.assertEqual(409, (await self.post("search_matches", team_query="Alpha")).status_code)

    async def test_unknown_stale_and_suspended_quotes_cannot_record(self):
        await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        for source_time, status in [(None, "active"), (self.now - timedelta(minutes=2), "active"), (self.now, "suspended")]:
            await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=$1,status=$2", source_time, status)
            quote = await self.read_market("fixture-1")
            self.assertFalse(quote["fresh"])
            result = await self.post("record_expert_signals", signals=[{"fixture_id": "fixture-1", "quote_id": quote["quote_id"], "evidence_text": "over"}])
            self.assertFalse(result.json()["accepted"])

    async def test_wrong_service_token_is_rejected(self):
        response = await self.client.post("/internal/mcp/search_matches", json=self.context(team_query="Alpha"), headers={"Authorization": "Bearer wrong"})
        self.assertEqual(401, response.status_code)

    async def test_unchanged_price_does_not_hide_changed_live_score_context(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        quote = await self.read_market(fixture_id)
        await self.pool.execute("UPDATE v2_fixtures SET phase='live',home_score=1,away_score=0 WHERE id=$1", fixture_id)
        response = await self.post('record_expert_signals', signals=[{'fixture_id':fixture_id, 'quote_id':quote['id'], 'evidence_text':'Alpha over'}])
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('quote_or_score_context_changed', response.json()['rejected'][0]['reason'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_quote_projection_rejects_older_event_and_closes_only_full_snapshot(self):
        await self.fixture()
        await self.fixture(source_updated_at=(self.now - timedelta(minutes=1)).isoformat(), received_at=(self.now - timedelta(minutes=1)).isoformat(), home_score=99)
        self.assertIsNone(await self.pool.fetchval("SELECT home_score FROM v2_fixtures LIMIT 1"))
        await self.fixture(markets=[], markets_complete=False)
        self.assertEqual("active", await self.pool.fetchval("SELECT status FROM v2_current_quotes LIMIT 1"))
        await self.fixture(markets=[], markets_complete=True)
        self.assertEqual("closed", await self.pool.fetchval("SELECT status FROM v2_current_quotes LIMIT 1"))

    async def test_batched_quotes_keep_newer_price_and_exact_full_snapshot_keys(self):
        await self.fixture()
        old=(self.now-timedelta(minutes=1)).isoformat()
        quote={"market_id":"total","market_name":"全场大小","kind":"total","period":"full_time",
               "line":"2.5","selection":"over","price":"2.5","source_updated_at":old,
               "received_at":self.now.isoformat()}
        other={**quote,"selection":"under","source_updated_at":self.now.isoformat()}
        await self.fixture(markets=[quote,other],markets_complete=True)
        rows=await self.pool.fetch("SELECT selection,price,status FROM v2_current_quotes ORDER BY selection")
        self.assertEqual(2,len(rows))
        self.assertEqual(Decimal("1.95"),rows[0]["price"])
        self.assertTrue(all(r["status"]=="active" for r in rows))
        await self.fixture(markets=[quote],markets_complete=True)
        self.assertEqual("active",await self.pool.fetchval("SELECT status FROM v2_current_quotes WHERE selection='over'"))
        self.assertEqual("closed",await self.pool.fetchval("SELECT status FROM v2_current_quotes WHERE selection='under'"))

    async def test_fixture_batch_skips_stale_snapshot_but_accepts_new_fixture(self):
        await self.fixture()
        old = self.now - timedelta(minutes=1)
        common = dict(sport="football", competition_name="League", home_team="Home",
                      away_team="Away", kickoff_at=self.now + timedelta(hours=1),
                      phase="scheduled", received_at=self.now, source_updated_at=self.now)
        stale = internal.FixtureInput.model_validate({**common, "id":"ignored-id", "source_id":"fixture-1",
                    "source_updated_at":old, "home_score":99, "markets_complete":True, "markets":[]})
        fresh = internal.FixtureInput.model_validate({**common, "id":"fixture-2", "source_id":"fixture-2",
                    "home_aliases":[" HOME "," "], "markets":[]})
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                self.assertEqual(1, await ingest_fixture_batch(conn, [fresh, stale]))
        self.assertEqual(2, await self.pool.fetchval("SELECT count(*) FROM v2_fixtures"))
        self.assertIsNone(await self.pool.fetchval("SELECT home_score FROM v2_fixtures WHERE source_id='fixture-1'"))
        self.assertEqual("active", await self.pool.fetchval("SELECT status FROM v2_current_quotes LIMIT 1"))
        self.assertEqual(["home"], await self.pool.fetchval("SELECT home_aliases FROM v2_fixtures WHERE source_id='fixture-2'"))

    async def test_duplicate_fixture_batch_rejected_before_any_writes(self):
        body = internal.FixtureInput.model_validate(dict(id="duplicate", source_id="duplicate",
            sport="football", competition_name="League", home_team="A", away_team="B",
            kickoff_at=self.now, phase="scheduled", received_at=self.now, markets=[]))
        from fastapi import HTTPException
        async with self.pool.acquire() as conn:
            with self.assertRaises(HTTPException) as rejected:
                await ingest_fixture_batch(conn, [body, body])
        self.assertEqual(422, rejected.exception.status_code)
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_fixtures"))

    async def test_legacy_mixed_case_and_padded_aliases_search_and_resolve_consistently(self):
        fixture_id = await self.fixture()
        await self.pool.execute("UPDATE v2_fixtures SET home_aliases=ARRAY['  LEGACY HOME  '],away_aliases=ARRAY[' Legacy Away '] WHERE id=$1", fixture_id)
        key, rows = await self.search_all('legacy home')
        self.assertEqual([fixture_id], [row['id'] for row in rows])
        resolved = await self.post('resolve_match', search_key=key, team_a=' Legacy Home ', team_b='LEGACY AWAY')
        self.assertEqual(200, resolved.status_code, resolved.text)
        self.assertTrue(resolved.json()['resolved'])

    async def test_alias_search_preserves_reserve_team_and_women_identity(self):
        first = await self.fixture(1, home_team='Club Women', away_team='Other Women', home_aliases=['俱乐部女足'])
        await self.fixture(2, home_team='Club U21', away_team='Other U21', home_aliases=['俱乐部青年队'])
        key, rows = await self.search_all('俱乐部女足')
        self.assertEqual([first], [row['id'] for row in rows])
        wrong = await self.post('resolve_match', search_key=key, team_a='Club U21', team_b='Other U21')
        self.assertFalse(wrong.json()['resolved'])


if __name__ == "__main__":
    unittest.main()
