import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import unittest
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

    async def search_all(self, team="Alpha", page_size=20):
        pages = []
        cursor = None
        while True:
            response = await self.post("search_matches", team_query=team, cursor=cursor, page_size=page_size)
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


if __name__ == "__main__":
    unittest.main()
