import asyncio
import base64
import hashlib
import hmac
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


class MarketPeriodInputTests(unittest.TestCase):
    def body(self, period):
        return reviews.MarketInput(review_id=uuid.uuid4(), attempt_id=uuid.uuid4(),
                                   fixture_id='fixture-period-test', period=period)

    def test_exact_chinese_periods_and_existing_full_aliases(self):
        for raw, expected in (('全场', 'full_time'), ('上半场', 'first_half'),
                              ('下半场', 'second_half'), ('full_match', 'full_time'),
                              ('full', 'full_time')):
            with self.subTest(raw=raw):
                self.assertEqual(expected, self.body(raw).period)

    def test_canonical_unknown_ascii_and_none_keep_existing_semantics(self):
        for period in ('full_time', 'first_half', 'second_half', 'unknown_full_match', None):
            with self.subTest(period=period):
                self.assertEqual(period, self.body(period).period)

    def test_ambiguous_or_unrecognized_chinese_period_is_rejected(self):
        for period in ('半场', '加时', '全场大小', '全场上半场', '全场;DROP TABLE', ''):
            with self.subTest(period=period), self.assertRaises(ValueError):
                self.body(period)

    def test_non_string_periods_remain_invalid(self):
        for period in ([], {}, 1, True):
            with self.subTest(period=period), self.assertRaises(ValueError):
                self.body(period)


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
        fixture_id = updates.pop('id', f"fixture-{index}")
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

    async def test_bound_fixture_read_preserves_native_fixture_fields(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        body = reviews.MarketInput(**self.context(fixture_id=fixture_id))
        async with self.pool.acquire() as conn:
            expected = dict(await conn.fetchrow('SELECT * FROM v2_fixtures WHERE id=$1', fixture_id))
            actual = await reviews._bound_active_fixture(conn, body)
        self.assertEqual(expected, actual)
        self.assertIsInstance(actual['kickoff_at'], datetime)

    async def test_invalid_fixture_binding_never_refreshes_source_and_keeps_error_priority(self):
        fixture_id = await self.fixture()
        self.client._transport.app.state.settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        factory = httpx.AsyncClient
        calls = []

        async def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={'accepted': True, 'reason': 'refresh_completed'})

        with patch.object(reviews.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            for candidate in (fixture_id, 'missing-fixture'):
                response = await self.post('list_match_markets', fixture_id=candidate)
                self.assertEqual(409, response.status_code)
                self.assertEqual('fixture must be uniquely resolved first', response.json()['detail'])
            key, _ = await self.search_all()
            await self.resolve(key)
            await self.pool.execute('UPDATE v2_review_fixture_bindings SET attempt_id=$2 WHERE job_id=$1', self.job, uuid.uuid4())
            response = await self.post('list_match_markets', fixture_id=fixture_id)
            self.assertEqual(409, response.status_code)
            self.assertEqual('fixture must be uniquely resolved first', response.json()['detail'])
        self.assertEqual([], calls)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))

    async def test_over_under_filter_alias_keeps_exact_period_line_and_direction(self):
        fixture_id = await self.fixture(markets=[
            {'market_id': market, 'market_name': market, 'kind': kind, 'period': period,
             'line': line, 'selection': selection, 'price': '1.95',
             'source_updated_at': self.now.isoformat(), 'received_at': self.now.isoformat()}
            for market, kind, period, line, selection in (
                ('total', 'total', 'full_time', '2.75', 'over'),
                ('total', 'total', 'full_time', '2.75', 'under'),
                ('half', 'total', 'first_half', '2.75', 'over'),
                ('handicap', 'handicap', 'full_time', '2.75', 'home'))])
        key, _ = await self.search_all()
        await self.resolve(key)
        response = await self.post('list_match_markets', fixture_id=fixture_id,
                                   market_kind='over_under', period='full_time', line='2.75', selection='over')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual(1, len(result['markets']))
        self.assertEqual('total', result['market_filter']['market_kind'])
        self.assertEqual(('total', 'full_time', 'over'),
                         tuple(result['markets'][0][key] for key in ('market_kind', 'period', 'selection')))
        self.assertEqual(Decimal('2.75'), Decimal(result['markets'][0]['line']))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))
        unknown = await self.post('list_match_markets', fixture_id=fixture_id, market_kind='unknown_over_under')
        self.assertEqual(200, unknown.status_code)
        self.assertEqual([], unknown.json()['markets'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))

    async def test_explicit_total_query_reads_exact_options_and_retains_source_evidence(self):
        fixture_id = await self.fixture(markets=[
            {'market_id': market, 'market_name': market, 'kind': kind, 'period': period,
             'line': line, 'selection': selection, 'price': '1.95',
             'source_updated_at': self.now.isoformat(), 'received_at': self.now.isoformat()}
            for market, kind, period, line, selection in (
                ('total', 'total', 'full_time', '2.5', 'over'),
                ('total', 'total', 'full_time', '2.5', 'under'),
                ('other', 'total', 'full_time', '3.5', 'over'),
                ('half', 'total', 'first_half', '2.5', 'over'),
                ('handicap', 'handicap', 'full_time', '2.5', 'home'))])
        key, _ = await self.search_all()
        await self.resolve(key)
        response = await self.post('list_match_markets', fixture_id=fixture_id, market_query=' 大２.５ ')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual({'market_kind': 'total', 'line': '2.5', 'selection': 'over'}, result['market_filter'])
        self.assertEqual(5, result['market_coverage']['total_quotes'])
        self.assertEqual(2, result['market_coverage']['matched_quotes'])
        self.assertEqual({'full_time', 'first_half'}, {q['period'] for q in result['markets']})
        self.assertTrue(all(q['selection'] == 'over' and Decimal(q['line']) == Decimal('2.5')
                            for q in result['markets']))
        self.assertEqual(2, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))
        parameters = await self.pool.fetchval("SELECT payload->'parameters' FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='mcp_list_match_markets'", self.job)
        self.assertEqual('大2.5', parameters['market_query'])

    async def test_conflicting_total_shorthand_does_not_broaden_authorized_quotes(self):
        fixture_id = await self.fixture(markets=[
            {'market_id': 'total', 'market_name': '全场大小', 'kind': 'total', 'period': 'full_time',
             'line': '2.5', 'selection': selection, 'price': '1.95',
             'source_updated_at': self.now.isoformat(), 'received_at': self.now.isoformat()}
            for selection in ('over', 'under')])
        key, _ = await self.search_all()
        await self.resolve(key)
        response = await self.post('list_match_markets', fixture_id=fixture_id, market_query='大2.5', selection='under')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual({'selection': 'under', 'market_query': '大2.5'}, result['market_filter'])
        self.assertEqual([], result['markets'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))

    async def test_full_match_filter_alias_preserves_native_line_and_away_direction(self):
        fixture_id = 'leyu:5635232'
        await self.fixture(id=fixture_id, source_id=fixture_id, markets=[
            {'market_id': market, 'market_name': market, 'kind': 'handicap', 'period': period,
             'line': '0.5', 'selection': selection, 'price': '1.95',
             'source_updated_at': self.now.isoformat(), 'received_at': self.now.isoformat()}
            for market, period, selection in (
                ('4', 'full_time', 'away'),
                ('4', 'full_time', 'home'),
                ('19', 'first_half', 'away'))])
        key, _ = await self.search_all()
        await self.resolve(key)
        response = await self.post('list_match_markets', fixture_id=fixture_id,
                                   market_kind='handicap', period='full_match', line='0.5', selection='away')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual(1, len(result['markets']))
        self.assertEqual('full_time', result['market_filter']['period'])
        self.assertEqual(('handicap', 'full_time', 'away'),
                         tuple(result['markets'][0][field] for field in ('market_kind', 'period', 'selection')))
        self.assertEqual(Decimal('0.5'), Decimal(result['markets'][0]['line']))
        self.assertEqual(Decimal('-0.5'), Decimal(result['markets'][0]['selection_line']))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))

    async def test_chinese_period_filters_keep_exact_period_line_and_direction(self):
        fixture_id = await self.fixture(markets=[
            {'market_id': period, 'market_name': period, 'kind': 'total', 'period': period,
             'line': line, 'selection': selection, 'price': '1.95',
             'source_updated_at': self.now.isoformat(), 'received_at': self.now.isoformat()}
            for period in ('full_time', 'first_half', 'second_half')
            for line, selection in (('2.5', 'over'), ('2.5', 'under'), ('3.5', 'over'))])
        key, _ = await self.search_all()
        await self.resolve(key)
        for raw, canonical in (('全场', 'full_time'), ('上半场', 'first_half'),
                               ('下半场', 'second_half')):
            with self.subTest(period=raw):
                response = await self.post('list_match_markets', fixture_id=fixture_id,
                                           market_kind='total', period=raw, line='2.5', selection='over')
                self.assertEqual(200, response.status_code, response.text)
                result = response.json()
                self.assertEqual(canonical, result['market_filter']['period'])
                self.assertEqual([(canonical, Decimal('2.5'), 'over')],
                                 [(q['period'], Decimal(q['line']), q['selection'])
                                  for q in result['markets']])
        self.assertEqual(3, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))
        unknown = await self.post('list_match_markets', fixture_id=fixture_id, period='半场')
        self.assertEqual(422, unknown.status_code)
        self.assertEqual(3, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))

    async def test_full_filter_alias_matches_full_time_without_expanding_half_or_direction(self):
        fixture_id = await self.fixture(markets=[
            {'market_id': market, 'market_name': market, 'kind': 'total', 'period': period,
             'line': '2.5', 'selection': selection, 'price': '1.95',
             'source_updated_at': self.now.isoformat(), 'received_at': self.now.isoformat()}
            for market, period, selection in (
                ('total', 'full_time', 'over'), ('total', 'full_time', 'under'),
                ('half', 'first_half', 'over'))])
        key, _ = await self.search_all()
        await self.resolve(key)
        response = await self.post('list_match_markets', fixture_id=fixture_id,
                                   market_kind='total', period='full', line='2.5', selection='over')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('full_time', response.json()['market_filter']['period'])
        self.assertEqual([('full_time', 'over')],
                         [(q['period'], q['selection']) for q in response.json()['markets']])
        unknown = await self.post('list_match_markets', fixture_id=fixture_id, period='unknown_full_match')
        self.assertEqual(200, unknown.status_code)
        self.assertEqual([], unknown.json()['markets'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))

    async def test_asian_handicap_filter_alias_preserves_native_period_and_selection(self):
        fixture_id = 'leyu:5635231'
        await self.fixture(id=fixture_id, source_id=fixture_id, markets=[
            {'market_id': market, 'market_name': market, 'kind': 'handicap', 'period': period,
             'line': '0.5', 'selection': selection, 'price': '1.95',
             'source_updated_at': self.now.isoformat(), 'received_at': self.now.isoformat()}
            for market, period, selection in (
                ('4', 'full_time', 'away'),
                ('4', 'full_time', 'home'),
                ('19', 'first_half', 'away'))])
        key, _ = await self.search_all()
        await self.resolve(key)
        response = await self.post('list_match_markets', fixture_id=fixture_id,
                                   market_kind='asian_handicap', period='full_time', line='0.5', selection='away')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual(1, len(result['markets']))
        self.assertEqual(('handicap', 'full_time', 'away'),
                         tuple(result['markets'][0][field] for field in ('market_kind', 'period', 'selection')))
        self.assertEqual('handicap', result['market_filter']['market_kind'])
        self.assertEqual(Decimal('0.5'), Decimal(result['markets'][0]['line']))
        self.assertEqual(Decimal('-0.5'), Decimal(result['markets'][0]['selection_line']))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))
        unknown = await self.post('list_match_markets', fixture_id=fixture_id, market_kind='unknown_asian_handicap')
        self.assertEqual(200, unknown.status_code)
        self.assertEqual([], unknown.json()['markets'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))

    async def test_fixture_ingestion_allows_audit_reference_but_serializes_writers(self):
        fixture_id = await self.fixture()
        body = internal.FixtureInput.model_validate({
            'id': fixture_id, 'source_id': fixture_id, 'sport': 'football',
            'competition_name': 'League', 'home_team': 'Alpha1', 'away_team': 'Beta1',
            'kickoff_at': (self.now+timedelta(hours=1)).isoformat(), 'phase': 'scheduled',
            'source_updated_at': (self.now+timedelta(seconds=1)).isoformat(),
            'received_at': (self.now+timedelta(seconds=1)).isoformat(),
            'markets': [{'market_id': 'total', 'market_name': '全场大小', 'kind': 'total',
                         'period': 'full_time', 'line': '2.5', 'selection': 'over',
                         'price': '2.10', 'source_updated_at': (self.now+timedelta(seconds=1)).isoformat(),
                         'received_at': (self.now+timedelta(seconds=1)).isoformat()}]})
        entered, release = asyncio.Event(), asyncio.Event()

        class PausedConnection:
            def __init__(self, conn):
                self.conn = conn

            def __getattr__(self, name):
                return getattr(self.conn, name)

            async def execute(self, query, *args):
                if 'INSERT INTO v2_current_quotes' in query:
                    entered.set()
                    await asyncio.wait_for(release.wait(), timeout=5)
                return await self.conn.execute(query, *args)

        async def update():
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    return await ingest_fixture_batch(PausedConnection(conn), [body])

        writer = asyncio.create_task(update())
        binding_error = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            async with self.pool.acquire() as conn:
                await conn.execute("SET lock_timeout='800ms'")
                try:
                    await conn.execute(
                        "INSERT INTO v2_review_fixture_bindings(job_id,attempt_id,fixture_id,search_key,evidence) "
                        "VALUES($1,$2,$3,'isolated-reference','{}')", self.job, self.attempt, fixture_id)
                except asyncpg.LockNotAvailableError as exc:
                    binding_error = type(exc).__name__
                await conn.execute("SET lock_timeout='100ms'")
                with self.assertRaises(asyncpg.LockNotAvailableError):
                    await conn.fetchrow('SELECT id FROM v2_fixtures WHERE id=$1 FOR NO KEY UPDATE', fixture_id)
        finally:
            release.set()
            written = await writer
        self.assertIsNone(binding_error, 'non-key fixture updates must allow audit references')
        self.assertEqual(1, written)
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(Decimal('2.10'), await self.pool.fetchval(
            'SELECT price FROM v2_current_quotes WHERE fixture_id=$1', fixture_id))

    async def test_source_fragment_candidates_preserve_scope_pagination_and_identity(self):
        home, away = '阿根廷农业U20', '塔勒雷斯雷梅迪奥斯U20'
        await self.fixture(1, home_team=home, away_team=away)
        await self.fixture(2, home_team=home, away_team=away, sport='basketball')
        await self.fixture(3, home_team=home, away_team=away,
                           kickoff_at=(self.now-timedelta(days=8)).isoformat())
        await self.fixture(4, home_team='阿根廷农业女足', away_team='另一女足')
        scope = dict(team_query='卡洛斯卡萨雷斯农业', sport='football', page_size=1,
                     from_utc=(self.now-timedelta(hours=12)).isoformat(),
                     to_utc=(self.now+timedelta(hours=72)).isoformat())
        response = await self.post('search_matches', **scope)
        self.assertEqual(200, response.status_code, response.text)
        first = response.json()
        self.assertEqual(2, first['total_candidates'])
        self.assertEqual('source_fragment_candidate', first['items'][0]['retrieval_basis'])
        blocked = await self.post('resolve_match', search_key=first['search_key'], team_a=home, team_b=away)
        self.assertEqual(409, blocked.status_code)
        second = await self.post('search_matches', **scope, cursor=first['next_cursor'])
        self.assertEqual(200, second.status_code, second.text)
        self.assertIsNone(second.json()['next_cursor'])
        items = first['items'] + second.json()['items']
        self.assertEqual({'fixture-1', 'fixture-4'}, {r['id'] for r in items})
        self.assertTrue(all(r['retrieval_basis']=='source_fragment_candidate' for r in items))
        missing = await self.post('resolve_match', search_key=first['search_key'],
                                  team_a=scope['team_query'], team_b=away)
        self.assertFalse(missing.json()['resolved'], 'retrieval fragments must not become identity aliases')
        resolved = await self.post('resolve_match', search_key=first['search_key'], team_a=home, team_b=away)
        self.assertTrue(resolved.json()['resolved'])
        self.assertEqual('fixture-1', resolved.json()['fixture']['id'])

    async def test_strict_chinese_search_does_not_mix_fragment_candidates(self):
        query = '卡洛斯卡萨雷斯农业'
        await self.fixture(1, home_team=query+'U20')
        await self.fixture(2, home_team='阿根廷农业U20')
        response = await self.post('search_matches', team_query=query, sport='football',
                                   from_utc=self.now.isoformat(),
                                   to_utc=(self.now+timedelta(days=3)).isoformat())
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(['fixture-1'], [r['id'] for r in response.json()['items']])
        self.assertNotIn('retrieval_basis', response.json()['items'][0])

    async def test_chinese_script_folding_search_and_resolve_both_directions(self):
        for index, home, away, competition, query, opponent, league in (
            (1, '莫斯科火车头', '莫斯科斯巴达', '俄罗斯超级联赛', '莫斯科火車頭', '莫斯科斯巴達', '俄羅斯超級聯賽'),
            (2, '莫斯科火車頭', '莫斯科斯巴達', '俄羅斯超級聯賽', '莫斯科火车头', '莫斯科斯巴达', '俄罗斯超级联赛'),
        ):
            await self.pool.execute('DELETE FROM v2_review_fixture_bindings')
            await self.fixture(index, home_team=home, away_team=away, competition_name=competition)
            start = self.now + timedelta(hours=index, minutes=-1)
            end = self.now + timedelta(hours=index, minutes=1)
            response = await self.post('search_matches', team_query=query, competition_query=league,
                                       sport='football', from_utc=start.isoformat(), to_utc=end.isoformat())
            self.assertEqual(200, response.status_code, response.text)
            result = response.json()
            self.assertEqual([f'fixture-{index}'], [item['id'] for item in result['items']])
            resolved = await self.post('resolve_match', search_key=result['search_key'], team_a=query,
                                       team_b=opponent, competition=league, from_utc=start.isoformat(), to_utc=end.isoformat())
            self.assertEqual(200, resolved.status_code, resolved.text)
            self.assertTrue(resolved.json()['resolved'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_chinese_script_folding_does_not_merge_women_or_ambiguous_fixtures(self):
        await self.fixture(1, home_team='莫斯科火车头', away_team='莫斯科斯巴达')
        await self.fixture(2, home_team='莫斯科火车头(女)', away_team='莫斯科斯巴达(女)')
        key, candidates = await self.search_all(team='莫斯科火車頭')
        self.assertEqual(2, len(candidates))
        result = await self.post('resolve_match', search_key=key, team_a='莫斯科火車頭', team_b='莫斯科斯巴達')
        self.assertTrue(result.json()['resolved'])
        self.assertEqual('fixture-1', result.json()['fixture']['id'])
        await self.fixture(3, home_team='莫斯科火車頭', away_team='莫斯科斯巴達')
        key, _ = await self.search_all(team='莫斯科火车头')
        result = await self.post('resolve_match', search_key=key, team_a='莫斯科火車頭', team_b='莫斯科斯巴達')
        self.assertEqual('ambiguous_fixture', result.json()['reason'])
        self.assertEqual(2, result.json()['candidate_count'])

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

    async def test_structured_market_filter_is_complete_and_only_authorizes_returned_quotes(self):
        fixture_id = await self.fixture(markets=[
            {"market_id": "total", "market_name": "全场大小", "kind": "total", "period": "full_time",
             "line": "2.5", "selection": "over", "price": "1.95", "source_updated_at": self.now.isoformat(),
             "received_at": self.now.isoformat()},
            {"market_id": "total", "market_name": "全场大小", "kind": "total", "period": "full_time",
             "line": "2.5", "selection": "under", "price": "1.91", "source_updated_at": self.now.isoformat(),
             "received_at": self.now.isoformat()},
            {"market_id": "total", "market_name": "上半场大小", "kind": "total", "period": "first_half",
             "line": "1.0", "selection": "over", "price": "1.88", "source_updated_at": self.now.isoformat(),
             "received_at": self.now.isoformat()},
            {"market_id": "handicap", "market_name": "全场亚洲让球", "kind": "handicap", "period": "full_time",
             "line": "0.75", "selection": "home", "price": "1.86", "source_updated_at": self.now.isoformat(),
             "received_at": self.now.isoformat()},
        ])
        key, _ = await self.search_all()
        self.assertTrue((await self.resolve(key))["resolved"])

        response = await self.post("list_match_markets", fixture_id=fixture_id, market_kind="total",
                                   period="full_time", line="2.50", selection="over",
                                   market_query="  全场大小  ")
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual(4, result["market_coverage"]["total_quotes"])
        self.assertEqual(1, result["market_coverage"]["matched_quotes"])
        self.assertEqual(1, result["market_coverage"]["returned_quotes"])
        self.assertTrue(result["market_coverage"]["complete_for_filter"])
        self.assertEqual("全场大小", result["market_filter"]["market_query"])
        self.assertEqual("over", result["markets"][0]["selection"])
        self.assertNotIn("price", result["markets"][0])

        unread_quote = await self.pool.fetchval(
            "SELECT id FROM v2_current_quotes WHERE fixture_id=$1 AND selection='under'", fixture_id)
        recorded = await self.post("record_expert_signals", signals=[{
            "fixture_id": fixture_id, "quote_id": str(unread_quote), "evidence_text": "全场小2.5"}])
        self.assertEqual(["quote_not_read_or_changed"], [row["reason"] for row in recorded.json()["rejected"]])
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_expert_signals"))

    async def test_unfiltered_market_read_keeps_full_fallback_inventory(self):
        fixture_id = await self.fixture(markets=[
            {"market_id": "total", "market_name": "全场大小", "kind": "total", "period": "full_time",
             "line": line, "selection": selection, "price": "1.95", "source_updated_at": self.now.isoformat(),
             "received_at": self.now.isoformat()}
            for line, selection in (("2.5", "over"), ("2.5", "under"), ("3.5", "over"))
        ])
        key, _ = await self.search_all()
        self.assertTrue((await self.resolve(key))["resolved"])
        response = await self.post("list_match_markets", fixture_id=fixture_id)
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual(3, result["market_coverage"]["total_quotes"])
        self.assertEqual(3, result["market_coverage"]["matched_quotes"])
        self.assertEqual(3, result["market_coverage"]["returned_quotes"])
        self.assertEqual(3, len(result["markets"]))

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
        self.assertEqual([], response.json()['rejected'])
        self.assertEqual('2.05000', response.json()['accepted'][0]['price'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        await self.read_market(fixture_id)
        retry = await self.post('record_expert_signals', signals=[pick])
        self.assertEqual(1, len(retry.json()['accepted']))
        self.assertEqual('2.05000', retry.json()['accepted'][0]['price'])

    async def test_fresh_market_read_uses_one_database_transaction_with_complete_receipt(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        queries = []

        async def init(conn):
            await conn.set_type_codec('jsonb', schema='pg_catalog', encoder=json.dumps, decoder=json.loads)
            conn.add_query_logger(lambda record: queries.append(record.query))

        await self.pool.close()
        self.pool = await asyncpg.create_pool(self.admin_url.rsplit('/', 1)[0] + '/' + self.dbname,
                                              min_size=1, max_size=1, init=init)
        database.pool = self.pool
        self.client._transport.app.state.settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        with patch.object(reviews, '_request_market_refresh', side_effect=AssertionError('fresh quotes need no I/O')):
            response = await self.post('list_match_markets', fixture_id=fixture_id)
        await asyncio.sleep(0)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, sum(query.startswith('BEGIN') for query in queries), queries)
        result = response.json()
        self.assertEqual('quotes_already_fresh', result['refresh']['reason'])
        quote = result['markets'][0]
        self.assertTrue(quote['can_record'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads WHERE job_id=$1', self.job))
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='mcp_list_match_markets' AND payload->>'status'='succeeded' AND payload ? 'response_snapshot'", self.job))
        read = await self.pool.fetchrow('SELECT price,quote_context FROM v2_review_market_reads WHERE job_id=$1', self.job)
        self.assertNotIn('price', quote)  # Price is persisted, not exposed for model matching.
        self.assertEqual(Decimal('1.95'), read['price'])
        self.assertEqual(fixture_id, read['quote_context']['fixture_id'])

    async def test_refresh_releases_single_pool_connection_and_rereads_updated_price(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '5 minutes'")
        await self.pool.close()

        async def init(conn):
            await conn.set_type_codec('jsonb', schema='pg_catalog', encoder=json.dumps, decoder=json.loads)

        self.pool = await asyncpg.create_pool(self.admin_url.rsplit('/', 1)[0] + '/' + self.dbname,
                                              min_size=1, max_size=1, init=init)
        database.pool = self.pool
        self.client._transport.app.state.settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        factory = httpx.AsyncClient

        async def handler(request):
            async with asyncio.timeout(2):
                await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now(),price=2.35")
            return httpx.Response(200, json={'accepted': True, 'reason': 'written'})

        with patch.object(reviews.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            response = await self.post('list_match_markets', fixture_id=fixture_id)
        self.assertEqual(200, response.status_code, response.text)
        self.assertNotIn('price', response.json()['markets'][0])
        self.assertTrue(response.json()['markets'][0]['fresh'])
        self.assertEqual(Decimal('2.35'), await self.pool.fetchval('SELECT price FROM v2_review_market_reads WHERE job_id=$1', self.job))

    async def test_author_disabled_during_refresh_cannot_commit_market_read_evidence(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '5 minutes'")
        self.client._transport.app.state.settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        factory = httpx.AsyncClient

        async def handler(request):
            await self.pool.execute('UPDATE v2_source_experts SET enabled=false WHERE source_id=$1 AND expert_id=$2', self.source, self.expert)
            return httpx.Response(200, json={'accepted': True, 'reason': 'written'})

        with patch.object(reviews.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            response = await self.post('list_match_markets', fixture_id=fixture_id)
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual('message or source is not eligible', response.json()['detail'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='mcp_list_match_markets'", self.job))

    async def test_filtered_market_does_not_refresh_for_unrelated_stale_quotes(self):
        fixture_id = await self.fixture(markets=[
            {"market_id": "total", "market_name": "全场大小", "kind": "total", "period": "full_time",
             "line": "2.5", "selection": "over", "price": "1.95", "source_updated_at": self.now.isoformat(),
             "received_at": self.now.isoformat()},
            {"market_id": "handicap", "market_name": "全场让球", "kind": "handicap", "period": "full_time",
             "line": "-0.5", "selection": "home", "price": "1.86", "source_updated_at": self.now.isoformat(),
             "received_at": self.now.isoformat()},
        ])
        key, _ = await self.search_all()
        await self.resolve(key)
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '5 minutes' WHERE fixture_id=$1 AND market_kind='handicap'", fixture_id)
        settings = self.client._transport.app.state.settings
        settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        calls = []
        factory = httpx.AsyncClient
        async def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={'accepted': True, 'reason': 'refresh_completed'})
        with patch.object(reviews.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            response = await self.post('list_match_markets', fixture_id=fixture_id,
                                       market_kind='total', period='full_time', selection='over')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual([], calls)
        self.assertEqual('quotes_already_fresh', result['refresh']['reason'])
        self.assertEqual(2, result['market_coverage']['total_quotes'])
        self.assertEqual(1, result['market_coverage']['returned_quotes'])
        self.assertTrue(result['markets'][0]['fresh'])

    async def test_filtered_market_zero_result_skips_unrelated_refresh_for_fallback(self):
        fixture_id = await self.fixture(markets=[
            {"market_id": "handicap", "market_name": "全场让球", "kind": "handicap", "period": "full_time",
             "line": "-0.5", "selection": "home", "price": "1.86", "source_updated_at": self.now.isoformat(),
             "received_at": self.now.isoformat()},
        ])
        key, _ = await self.search_all()
        await self.resolve(key)
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '5 minutes' WHERE fixture_id=$1", fixture_id)
        settings = self.client._transport.app.state.settings
        settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        calls = []
        factory = httpx.AsyncClient
        async def handler(request):
            calls.append(request)
            return httpx.Response(200, json={'accepted': True, 'reason': 'refresh_completed'})
        with patch.object(reviews.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            response = await self.post('list_match_markets', fixture_id=fixture_id,
                                       market_kind='winner', period='full_time', selection='home')
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual([], calls)
        self.assertEqual('no_quotes_for_filter', result['refresh']['reason'])
        self.assertEqual(1, result['market_coverage']['total_quotes'])
        self.assertEqual(0, result['market_coverage']['returned_quotes'])
        self.assertEqual([], result['markets'])

    async def test_filtered_market_refreshes_when_matching_quote_is_stale(self):
        fixture_id = await self.fixture(markets=[
            {"market_id": "total", "market_name": "全场大小", "kind": "total", "period": "full_time",
             "line": "2.5", "selection": "over", "price": "1.95", "source_updated_at": self.now.isoformat(),
             "received_at": self.now.isoformat()},
            {"market_id": "handicap", "market_name": "全场让球", "kind": "handicap", "period": "full_time",
             "line": "-0.5", "selection": "home", "price": "1.86", "source_updated_at": self.now.isoformat(),
             "received_at": self.now.isoformat()},
        ])
        key, _ = await self.search_all()
        await self.resolve(key)
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '5 minutes' WHERE fixture_id=$1 AND market_kind='total'", fixture_id)
        settings = self.client._transport.app.state.settings
        settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        calls = []
        factory = httpx.AsyncClient
        async def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={'accepted': True, 'reason': 'refresh_completed'})
        with patch.object(reviews.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            response = await self.post('list_match_markets', fixture_id=fixture_id,
                                       market_kind='total', period='full_time', selection='over')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([{'fixture_id': fixture_id, 'source_match_id': fixture_id}], calls)
        self.assertTrue(response.json()['refresh']['accepted'])
        self.assertEqual(1, response.json()['market_coverage']['returned_quotes'])
        self.assertFalse(response.json()['markets'][0]['fresh'])

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

    async def test_near_expiry_confirmation_refreshes_before_final_write(self):
        await self._near_expiry_confirmation(refresh_available=True)

    async def test_near_expiry_confirmation_cannot_record_after_failed_refresh(self):
        await self._near_expiry_confirmation(refresh_available=False)

    async def test_near_expiry_source_price_refreshes_before_final_write(self):
        await self._near_expiry_confirmation(refresh_available=True, source_recent=True)

    async def _near_expiry_confirmation(self, *, refresh_available, source_recent=False):
        from app.quote_observation import CONTEXT_FIELDS, DETAIL_PATH, confirmation_for
        from app.review_state import fresh_quote
        base = datetime.now(timezone.utc)
        source_time = base if source_recent else self.now-timedelta(hours=1)
        fixture_id = await self.fixture(markets=[{'market_id': '4', 'market_name': '全场让球',
            'kind': 'handicap', 'period': 'full_time', 'line': '-0.5', 'selection': 'home',
            'price': '1.95', 'status': 'active',
            'source_updated_at': source_time.isoformat(),
            'received_at': self.now.isoformat(), 'play_id': '4',
            'market_instance_id': 'native-market', 'selection_id': 'native-option'}])
        key, _ = await self.search_all()
        await self.resolve(key)
        settings = self.client._transport.app.state.settings
        settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        settings.quote_max_age_seconds = 15
        clock = [base]
        observations = []
        record_age_reads = []
        recording = [False]
        factory = httpx.AsyncClient

        async def handler(request):
            self.assertEqual(self.pool.get_size(), self.pool.get_idle_size())
            if recording[0] and not refresh_available:
                observations.append(None)
                return httpx.Response(503, json={'detail': 'isolated refresh unavailable'})
            q = dict(await self.pool.fetchrow('SELECT * FROM v2_current_quotes WHERE fixture_id=$1', fixture_id))
            f = dict(await self.pool.fetchrow('SELECT * FROM v2_fixtures WHERE id=$1', fixture_id))
            observation = {'path': DETAIL_PATH, 'started_at': clock[0].isoformat(),
                'completed_at': clock[0].isoformat(), 'fixture_id': fixture_id,
                'source_match_id': fixture_id, 'context': {k: f[k] for k in CONTEXT_FIELDS},
                'quotes': [json.loads(json.dumps(q, default=str))]}
            observations.append(observation)
            return httpx.Response(200, json={'accepted': False,
                'reason': 'parent_unchanged_recheck_quotes', 'snapshot_committed': True,
                'current_observation': observation})

        async def age_budget(settings, connection=None):
            if recording[0]:
                record_age_reads.append(True)
                # No wall-clock sleeps: real SQL separates the two validations.
                # The original witness has 3s left before the final 4s of work.
                clock[0] = base+timedelta(seconds=12 if len(record_age_reads) == 1 else 16)
            return 15

        def confirm(observation, quote, fixture, max_age):
            return confirmation_for(observation, quote, fixture, max_age, now=clock[0])

        def fresh(quote, max_age):
            return fresh_quote(quote, max_age, now=clock[0])

        with patch.object(reviews.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)), \
                patch.object(reviews, 'runtime_quote_max_age', side_effect=age_budget), \
                patch.object(reviews, 'confirmation_for', side_effect=confirm), \
                patch.object(reviews, 'fresh_quote', side_effect=fresh):
            quote = await self.read_market(fixture_id)
            self.assertTrue(quote['can_record'])
            recording[0] = True
            result = await self.post('record_expert_signals', signals=[{'fixture_id': fixture_id,
                'quote_id': quote['quote_id'], 'evidence_text': 'Alpha1 -0.5'}])
        self.assertEqual(1 if source_recent else 2, len(observations),
                         'near-expiry witness needs a new precise read')
        if not refresh_available:
            self.assertEqual([], result.json()['accepted'], result.text)
            self.assertEqual('quote_inactive_unknown_or_stale', result.json()['rejected'][0]['reason'])
            self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
            self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))
            return
        self.assertEqual([], result.json()['rejected'], result.text)
        self.assertEqual(1, len(result.json()['accepted']), result.text)
        stored = await self.pool.fetchrow('SELECT * FROM v2_expert_signals')
        self.assertEqual(source_time, stored['quote_time'])
        evidence = await self.pool.fetchval('SELECT evidence FROM v2_quote_evidence')
        self.assertEqual((base+timedelta(seconds=12)).isoformat(),
                         evidence['current_confirmation']['started_at'])

    async def test_record_rechecks_expired_observation_and_records_current_price(self):
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
        self.assertEqual([],result.json()['rejected'])
        self.assertEqual('2.01000',result.json()['accepted'][0]['price'])
        stored=await self.pool.fetchrow('SELECT price,evidence FROM v2_quote_evidence')
        self.assertEqual(Decimal('2.01'),stored['price'])
        self.assertEqual(Decimal('1.95'),Decimal(stored['evidence']['reviewed_price']))

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

    async def test_inflight_legacy_search_continues_with_short_cursor_without_skipping_or_replay(self):
        for index in range(3):
            await self.fixture(index)

        def legacy(key, review_id, attempt_id, search_key, position):
            payload = json.dumps([str(review_id), str(attempt_id), search_key, position], separators=(',', ':')).encode()
            return base64.urlsafe_b64encode(payload + hmac.new(key, payload, hashlib.sha256).digest()).decode().rstrip('=')

        with patch('app.routes.reviews.encode_cursor', side_effect=legacy):
            first = await self.post('search_matches', team_query='Alpha', page_size=1)
        self.assertEqual(200, first.status_code, first.text)
        old = first.json()['next_cursor']
        self.assertGreater(len(old), 200)
        second = await self.post('search_matches', team_query='Alpha', page_size=1, cursor=old)
        self.assertEqual(200, second.status_code, second.text)
        short = second.json()['next_cursor']
        self.assertEqual(57, len(short))
        replay = await self.post('search_matches', team_query='Alpha', page_size=1, cursor=old)
        self.assertEqual(409, replay.status_code)
        third = await self.post('search_matches', team_query='Alpha', page_size=1, cursor=short)
        self.assertEqual(200, third.status_code, third.text)
        self.assertTrue(third.json()['search_complete'])
        self.assertIsNone(third.json()['next_cursor'])
        items = [item['id'] for response in (first, second, third) for item in response.json()['items']]
        self.assertEqual({'fixture-0', 'fixture-1', 'fixture-2'}, set(items))
        self.assertEqual(3, len(items))

    async def test_continuation_inherits_omitted_filters_without_widening_snapshot(self):
        for index in range(1, 4):
            await self.fixture(index)
        await self.fixture(4, sport='basketball')
        await self.fixture(5, competition_name='Other')
        await self.fixture(6, kickoff_at=(self.now + timedelta(days=9)).isoformat())
        first = await self.post('search_matches', team_query='Alpha', page_size=1,
                                sport='football', competition_query='League',
                                from_utc=self.now.isoformat(), to_utc=(self.now + timedelta(days=1)).isoformat())
        self.assertEqual(200, first.status_code, first.text)
        result = first.json()
        self.assertEqual(3, result['total_candidates'])
        cursor = result['next_cursor']
        for extra in ({'sport': 'basketball'}, {'competition_query': 'Other'},
                      {'to_utc': (self.now + timedelta(days=10)).isoformat()},
                      {'from_utc': (self.now - timedelta(days=1)).isoformat()},
                      {'team_query': 'Beta'}, {'phase': 'scheduled'}):
            conflict = await self.post('search_matches', **{'team_query': 'Alpha', 'cursor': cursor, **extra})
            self.assertEqual(409, conflict.status_code, conflict.text)
        second = await self.post('search_matches', team_query='Alpha', page_size=1, cursor=cursor)
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual(result['search_key'], second.json()['search_key'])
        replay = await self.post('search_matches', team_query='Alpha', page_size=1, cursor=cursor)
        self.assertNotEqual(200, replay.status_code)
        third = await self.post('search_matches', team_query='Alpha', page_size=1, cursor=second.json()['next_cursor'])
        self.assertEqual(200, third.status_code, third.text)
        self.assertTrue(third.json()['search_complete'])
        ids = [item['id'] for response in (first, second, third) for item in response.json()['items']]
        self.assertEqual(['fixture-1', 'fixture-2', 'fixture-3'], ids)
        evidence = await self.pool.fetchval(
            "SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='mcp_search_matches' "
            "AND payload->>'status'='succeeded' ORDER BY sequence_no DESC LIMIT 1", self.job)
        self.assertEqual('football', evidence['scope']['sport'])
        self.assertIsNotNone(evidence['scope']['from'])
        self.assertIsNone(evidence['parameters']['from_utc'])

    async def test_omitted_filters_fail_closed_without_authenticated_original_scope(self):
        await self.fixture(1)
        await self.fixture(2)
        first = await self.post('search_matches', team_query='Alpha', page_size=1, sport='football')
        self.assertEqual(200, first.status_code, first.text)
        # Simulate incomplete restored evidence only in this temporary database.
        await self.pool.execute("UPDATE v2_analysis_traces SET payload=payload-'scope' WHERE job_id=$1", self.job)
        response = await self.post('search_matches', team_query='Alpha', cursor=first.json()['next_cursor'])
        self.assertEqual(409, response.status_code)
        exact = await self.post('search_matches', team_query='Alpha', sport='football', cursor=first.json()['next_cursor'])
        self.assertEqual(200, exact.status_code, exact.text)

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

    async def test_basketball_intent_resolves_own_sport_and_records_selected_away_line(self):
        home, away = '金州勇士', '迈阿密热火'
        market = {'market_id': '39', 'market_name': '全场让分', 'kind': 'handicap',
                  'period': 'full_time', 'line': '-6.5', 'selection': 'away', 'price': '2.03',
                  'source_updated_at': self.now.isoformat(), 'received_at': self.now.isoformat()}
        fixture_id = await self.fixture(1, id='leyu:900000000001', sport='basketball',
                                        home_team=home, away_team=away, markets=[market])
        await self.fixture(2, sport='football', home_team='迈阿密国际', away_team='另一足球队')
        scope = dict(team_query=away, sport='basketball', team_a=home, team_b=away,
                     from_utc=(self.now-timedelta(hours=12)).isoformat(),
                     to_utc=(self.now+timedelta(hours=72)).isoformat())
        response = await self.post('search_matches', **scope)
        self.assertEqual(200, response.status_code, response.text)
        searched = response.json()
        self.assertTrue(searched['search_complete'])
        self.assertIsNone(searched['next_cursor'])
        self.assertEqual([fixture_id], [item['id'] for item in searched['items']])
        self.assertTrue(searched['resolution']['resolved'])
        self.assertTrue(searched['resolution']['fixture']['realtime_eligible'])
        result = await self.post('list_match_markets', fixture_id=fixture_id,
                                 market_kind='handicap', period='full_time', selection='away')
        self.assertEqual(200, result.status_code, result.text)
        quote = result.json()['markets'][0]
        self.assertEqual('away', quote['selection'])
        self.assertEqual(Decimal('6.5'), Decimal(quote['selection_line']))
        self.assertEqual(Decimal('-6.5'), Decimal(quote['line']))
        pick = dict(fixture_id=fixture_id, quote_id=quote['quote_id'],
                    evidence_text='推荐：迈阿密热火+6.5分；原文倍率不参与匹配')
        recorded = await self.post('record_expert_signals', signals=[pick])
        self.assertEqual(200, recorded.status_code, recorded.text)
        self.assertEqual([], recorded.json()['rejected'])
        self.assertEqual(fixture_id, recorded.json()['accepted'][0]['fixture_id'])
        self.assertEqual(Decimal('2.03'), Decimal(recorded.json()['accepted'][0]['price']))
        self.assertEqual(Decimal('6.5'), Decimal(recorded.json()['accepted'][0]['selection_line']))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_signal_requires_market_read_and_uses_current_system_price(self):
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
        self.assertEqual([],result.json()['rejected'])
        self.assertEqual('2.05000',result.json()['accepted'][0]['price'])
        await self.read_market(fixture_id)
        result = await self.post("record_expert_signals", signals=[pick])
        self.assertEqual(Decimal('2.05'), Decimal(result.json()["accepted"][0]["price"]))
        duplicate = await self.post("record_expert_signals", signals=[pick])
        self.assertTrue(duplicate.json()["accepted"][0]["duplicate"])
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_quote_evidence"))
        traces = await self.pool.fetch("SELECT payload FROM v2_analysis_traces WHERE trace_kind LIKE 'mcp_%'")
        self.assertTrue(traces)
        self.assertTrue(all(row['payload']['elapsed_ms'] >= 0 for row in traces))

    async def test_price_and_source_time_refresh_keep_intent_and_preserve_first_snapshot(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        quote = await self.read_market(fixture_id)
        original = await self.pool.fetchrow('SELECT * FROM v2_current_quotes')
        await self.pool.execute('UPDATE v2_current_quotes SET price=2.08,source_updated_at=now()')
        pick = {'fixture_id':fixture_id, 'quote_id':quote['quote_id'], 'evidence_text':'Alpha 大2.5 @9.99'}
        result = await self.post('record_expert_signals', signals=[pick])
        self.assertEqual([], result.json()['rejected'])
        self.assertEqual('2.08000', result.json()['accepted'][0]['price'])
        evidence = await self.pool.fetchrow('SELECT price,source_updated_at,evidence FROM v2_quote_evidence')
        self.assertEqual(Decimal('2.08'), evidence['price'])
        self.assertEqual(Decimal('1.95'), Decimal(evidence['evidence']['reviewed_price']))
        self.assertEqual(original['source_updated_at'].isoformat(), evidence['evidence']['reviewed_source_updated_at'])
        await self.pool.execute('UPDATE v2_current_quotes SET price=2.20,source_updated_at=now()')
        duplicate = await self.post('record_expert_signals', signals=[pick])
        self.assertTrue(duplicate.json()['accepted'][0]['duplicate'])
        self.assertEqual('2.08000',duplicate.json()['accepted'][0]['price'])
        self.assertEqual(Decimal('2.08'), await self.pool.fetchval('SELECT recommendation_price FROM v2_expert_signals'))
        self.assertEqual(evidence, await self.pool.fetchrow('SELECT price,source_updated_at,evidence FROM v2_quote_evidence'))

    async def test_current_quote_repricing_cannot_change_read_market_identity(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        quote = await self.read_market(fixture_id)
        original = await self.pool.fetchrow('SELECT * FROM v2_current_quotes')
        changes = {'line':Decimal('2.75'), 'selection':'under', 'period':'first_half',
                   'market_id':'different-market', 'play_id':'different-play',
                   'market_instance_id':'different-instance', 'selection_id':'different-selection',
                   'score_basis':'remaining_period'}
        for field, value in changes.items():
            with self.subTest(field=field):
                # Field names come only from this fixed test allowlist.
                await self.pool.execute(f'UPDATE v2_current_quotes SET {field}=$1,price=2.08,source_updated_at=now()',value)
                result = await self.post('record_expert_signals', signals=[{'fixture_id':fixture_id,
                    'quote_id':quote['quote_id'], 'evidence_text':'Alpha 大2.5'}])
                self.assertEqual([],result.json()['accepted'])
                self.assertEqual('quote_or_score_context_changed',result.json()['rejected'][0]['reason'])
                await self.pool.execute(f'UPDATE v2_current_quotes SET {field}=$1',original[field])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_price_change_does_not_allow_stale_or_suspended_current_quote(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        quote = await self.read_market(fixture_id)
        for source_time,status in [(None,'active'),(self.now-timedelta(minutes=5),'active'),(self.now,'suspended')]:
            await self.pool.execute('UPDATE v2_current_quotes SET price=2.08,source_updated_at=$1,status=$2',source_time,status)
            result = await self.post('record_expert_signals', signals=[{'fixture_id':fixture_id,
                'quote_id':quote['quote_id'], 'evidence_text':'Alpha 大2.5'}])
            self.assertEqual([],result.json()['accepted'])
            self.assertEqual('quote_inactive_unknown_or_stale',result.json()['rejected'][0]['reason'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))

    async def test_expert_odds_text_does_not_gate_market_match_or_override_system_price(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        quote = await self.read_market(fixture_id)

        result = await self.post('record_expert_signals', signals=[{
            'fixture_id': fixture_id,
            'quote_id': quote['quote_id'],
            'evidence_text': 'Alpha1 vs Beta1 全场大2.5，原文倍率 3.80',
        }])

        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual(1, len(result.json()['accepted']))
        self.assertEqual('1.95000', result.json()['accepted'][0]['price'])
        stored = await self.pool.fetchrow('SELECT recommendation_price FROM v2_expert_signals WHERE message_id=$1', self.message)
        self.assertEqual(Decimal('1.95000'), stored['recommendation_price'])

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

    async def test_multi_pick_quote_reads_release_fixture_before_writer_budget(self):
        markets = [{'market_id': 'total', 'market_name': '全场大小', 'kind': 'total',
                    'period': 'full_time', 'line': str(Decimal('2.5')+Decimal(i)/4),
                    'selection': 'over', 'price': '1.95',
                    'source_updated_at': self.now.isoformat(), 'received_at': self.now.isoformat()}
                   for i in range(8)]
        fixture_id = await self.fixture(markets=markets)
        key, _ = await self.search_all()
        await self.resolve(key)
        await self.read_market(fixture_id)
        ids = await self.pool.fetch('SELECT id FROM v2_current_quotes ORDER BY line')
        picks = [{'fixture_id': fixture_id, 'quote_id': str(row['id']),
                  'evidence_text': 'Alpha1 Beta1 大球'} for row in ids]
        locked = asyncio.Event()
        real_pool = self.pool

        class RemoteConnection:
            def __init__(self, conn):
                self.conn = conn

            def __getattr__(self, name):
                return getattr(self.conn, name)

            async def fetch(self, query, *args):
                if 'FOR SHARE OF q' in query:
                    await asyncio.sleep(.15)
                result = await self.conn.fetch(query, *args)
                if query.startswith('SELECT f.id') and 'FOR SHARE OF f' in query:
                    locked.set()
                return result

            async def fetchrow(self, query, *args):
                if 'FOR SHARE OF q' in query:
                    await asyncio.sleep(.15)
                return await self.conn.fetchrow(query, *args)

        class RemoteAcquire:
            async def __aenter__(self):
                self.acquisition = real_pool.acquire()
                return RemoteConnection(await self.acquisition.__aenter__())

            async def __aexit__(self, *args):
                return await self.acquisition.__aexit__(*args)

        class RemotePool:
            def acquire(self):
                return RemoteAcquire()

        update_error = None
        with patch.object(database, 'current_pool', return_value=RemotePool()):
            recording = asyncio.create_task(self.post('record_expert_signals', signals=picks))
            try:
                await asyncio.wait_for(locked.wait(), timeout=3)
                async with real_pool.acquire() as conn:
                    await conn.execute("SET lock_timeout='800ms'")
                    try:
                        await conn.execute('UPDATE v2_fixtures SET received_at=received_at WHERE id=$1', fixture_id)
                    except asyncpg.LockNotAvailableError as exc:
                        update_error = type(exc).__name__
            finally:
                response = await recording
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(8, len(response.json()['accepted']))
        self.assertEqual([], response.json()['rejected'])
        self.assertIsNone(update_error, 'quote round trips must not exhaust the collector lock budget')
        self.assertEqual(8, await real_pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))

    async def test_batch_quote_pairs_preserve_cross_fixture_rejections_and_duplicates(self):
        for index in (1, 2, 3):
            await self.fixture(index)
        key, _ = await self.search_all()
        quotes = {}
        for index in (1, 2):
            await self.resolve(key, index)
            quotes[index] = (await self.read_market(f'fixture-{index}'))['quote_id']
        quotes[3] = str(await self.pool.fetchval(
            "SELECT id FROM v2_current_quotes WHERE fixture_id='fixture-3'"))
        pairs = [(1, 1), (1, 2), (2, 1), (2, 2), (1, 1), (3, 3)]
        response = await self.post('record_expert_signals', signals=[{
            'fixture_id': f'fixture-{fixture}', 'quote_id': quotes[quote],
            'evidence_text': 'Alpha Beta 大球 @9.99'} for fixture, quote in pairs])
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual([0, 3, 4], [pick['index'] for pick in result['accepted']])
        self.assertEqual([False, False, True], [pick['duplicate'] for pick in result['accepted']])
        self.assertEqual([1, 2, 5], [pick['index'] for pick in result['rejected']])
        self.assertTrue(all(pick['reason']=='quote_not_read_or_changed' for pick in result['rejected']))
        self.assertEqual(2, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(2, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))

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
        response = await self.post('record_expert_signals', signals=[{'fixture_id':fixture_id, 'quote_id':quote['quote_id'], 'evidence_text':'Alpha over'}])
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

    async def test_common_abbreviations_and_clear_prefixes_resolve_to_standard_fixture(self):
        fixture_id = await self.fixture(1, home_team='Manchester United', away_team='Tottenham Hotspur')
        key, rows = await self.search_all('Man Utd')
        self.assertEqual([fixture_id], [row['id'] for row in rows])

        response = await self.post('resolve_match', search_key=key,
                                   team_a='Man Utd', team_b='Tottenham')
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()['resolved'])
        self.assertEqual(fixture_id, response.json()['fixture']['id'])

    async def test_search_and_resolution_fold_accents_and_punctuation(self):
        fixture_id = await self.fixture(1, home_team='München 1860', away_team='Racing Club de Avellaneda')
        key, rows = await self.search_all('Munchen-1860')
        self.assertEqual([fixture_id], [row['id'] for row in rows])

        response = await self.post('resolve_match', search_key=key,
                                   team_a='Munchen 1860', team_b='Racing Club de Avellaneda')
        self.assertTrue(response.json()['resolved'])

    async def test_generic_single_word_prefix_does_not_bind_to_a_club(self):
        await self.fixture(1, home_team='Manchester United', away_team='Tottenham Hotspur')
        key, rows = await self.search_all('United')
        self.assertEqual(1, len(rows))

        response = await self.post('resolve_match', search_key=key,
                                   team_a='United', team_b='Tottenham')
        self.assertFalse(response.json()['resolved'])
        self.assertEqual('no_matching_fixture', response.json()['reason'])

    async def test_flexible_team_prefix_keeps_same_pair_ambiguous(self):
        await self.fixture(1, home_team='Bayern Munich', away_team='Borussia Dortmund')
        await self.fixture(2, home_team='Bayern Leverkusen', away_team='Borussia Dortmund', phase='finished')
        key, rows = await self.search_all('Bayern')
        self.assertEqual(2, len(rows))

        response = await self.post('resolve_match', search_key=key,
                                   team_a='Bayern', team_b='Borussia Dortmund')
        self.assertFalse(response.json()['resolved'])
        self.assertEqual('ambiguous_fixture', response.json()['reason'])
        self.assertEqual(2, response.json()['candidate_count'])


if __name__ == "__main__":
    unittest.main()
