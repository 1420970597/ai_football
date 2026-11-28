"""Restricted application uses the existing tools with a real least-privilege role."""
import os
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

import asyncpg
from fastapi import HTTPException
import httpx

from app import database, mcp_app
from app.config import load_settings
from app.runtime_settings import quote_max_age_from_rows
from app.security import encrypt_secret
from app.review_quote_policy import sync_quote_policy
from infra.db.provision_mcp import provision, verify
from infra.db.provision_runtime import ProvisionError
import test_reviews as review_fixture


class LimitedConfigurationTests(unittest.TestCase):
    def test_only_service_credentials_are_consumed(self):
        env = {'DATABASE_URL': 'postgresql://unused@isolated/test',
               'EXPERT_MCP_TOKEN': 'isolated-tools', 'COLLECTOR_INTERNAL_TOKEN': 'isolated-collector',
               'APP_SESSION_SECRET': 'must-not-be-read', 'DATA_ENCRYPTION_KEY': 'must-not-be-read',
               'HERMES_API_KEY': 'must-not-be-read', 'EXECUTOR_INTERNAL_TOKEN': 'must-not-be-read',
               'BOOTSTRAP_ADMIN_PASSWORD': 'must-not-be-read', 'LIVE_ORDERS_ENABLED': 'true'}
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings(role='mcp')
        for name in ('session_secret', 'encryption_key'):
            self.assertEqual(b'', getattr(settings, name))
        for name in ('hermes_api_key', 'executor_token', 'bootstrap_admin_password', 'bootstrap_admin_email'):
            self.assertEqual('', getattr(settings, name))
        self.assertTrue(settings.quote_policy_projection)

    def test_public_policy_is_not_accepted_by_ordinary_api(self):
        row = {'setting_key': 'quote_max_age_seconds', 'encrypted_value': 'mcp-quote-policy-v1:15'}
        with self.assertRaises(HTTPException):
            quote_max_age_from_rows(SimpleNamespace(encryption_key=b'k'*32), [row])
        self.assertEqual(15, quote_max_age_from_rows(SimpleNamespace(quote_policy_projection=True), [row]))

    def test_only_existing_tools_registered(self):
        self.assertEqual({'/healthz', '/readyz', '/internal/mcp/search_matches',
                          '/internal/mcp/resolve_match', '/internal/mcp/list_match_markets',
                          '/internal/mcp/record_expert_signals'}, set(mcp_app.create_app().openapi()['paths']))


class LimitedLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_budget_is_rejected_before_connecting(self):
        with patch.object(database.asyncpg, 'create_pool', new=AsyncMock()) as opened:
            for values in ({'min_size': 0}, {'max_size': 25}, {'min_size': 8, 'max_size': 4},
                           {'connect_timeout': 0}, {'connect_timeout': 31}):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    await database.open_pool(SimpleNamespace(database_url='unused'), **values)
        opened.assert_not_awaited()

    async def test_startup_and_shutdown_have_bounded_pool_without_admin_bootstrap(self):
        settings = SimpleNamespace(database_url='isolated')
        app = mcp_app.create_app()
        with patch.object(mcp_app, 'load_settings', return_value=settings) as load, \
                patch.object(database, 'open_pool', new=AsyncMock()) as opened, \
                patch.object(database, 'close_pool', new=AsyncMock()) as closed, \
                patch('app.auth.bootstrap_admin', new=AsyncMock(side_effect=AssertionError('must not bootstrap'))):
            with self.assertRaisesRegex(RuntimeError, 'cancel lifecycle'):
                async with mcp_app.lifespan(app):
                    self.assertIs(settings, app.state.settings)
                    raise RuntimeError('cancel lifecycle')
        load.assert_called_once_with(role='mcp')
        opened.assert_awaited_once_with(settings, min_size=1, max_size=8, connect_timeout=5)
        closed.assert_awaited_once_with()


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class LimitedMCPDatabaseTests(unittest.IsolatedAsyncioTestCase):
    context = review_fixture.ReviewTests.context
    post = review_fixture.ReviewTests.post
    fixture = review_fixture.ReviewTests.fixture
    search_all = review_fixture.ReviewTests.search_all
    resolve = review_fixture.ReviewTests.resolve

    async def asyncSetUp(self):
        await review_fixture.ReviewTests.asyncSetUp(self)
        await self.client.aclose()
        self.owner_pool = self.pool
        self.role = 'v2_runtime_mcp_' + uuid.uuid4().hex[:16]
        async with self.owner_pool.acquire() as conn:
            result = await provision(conn, self.role, 'isolated-mcp-test-password-at-least-32-characters')
        self.assertTrue(result['verified'])
        url = self.admin_url.rsplit('/', 1)[0] + '/' + self.dbname
        self.restricted = await asyncpg.create_pool(url, user=self.role, min_size=1, max_size=4,
                                                  init=self._codecs)
        self.settings = SimpleNamespace(mcp_internal_token='isolated-tools', collector_token='isolated-collector',
                                        quote_max_age_seconds=60, quote_policy_projection=True,
                                        market_refresh_url='http://unused.invalid/leyu/refresh')
        app = mcp_app.create_app()
        app.state.settings = self.settings
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                       headers={'Authorization': 'Bearer isolated-tools'})

    async def _codecs(self, conn):
        import json
        await conn.set_type_codec('jsonb', schema='pg_catalog', encoder=json.dumps, decoder=json.loads)

    async def asyncTearDown(self):
        database.pool = self.owner_pool
        await self.restricted.close()
        self.pool = self.owner_pool
        await review_fixture.ReviewTests.asyncTearDown(self)
        # Database drop removes the role's per-database grants/configuration.
        admin = await asyncpg.connect(self.admin_url)
        try:
            await admin.execute('DROP ROLE "' + self.role + '"')
        finally:
            await admin.close()

    async def test_real_four_tools_use_restricted_role_and_keep_price(self):
        fixture = await self.fixture()
        database.pool = self.restricted
        key, items = await self.search_all(page_size=1)
        self.assertEqual(1, len(items))
        self.assertTrue((await self.resolve(key))['resolved'])
        response = await self.post('list_match_markets', fixture_id=fixture, period='full_time',
                                   market_kind='total', line='2.5', selection='over')
        self.assertEqual(200, response.status_code, response.text)
        quote = response.json()['markets'][0]['quote_id']
        response = await self.post('record_expert_signals', signals=[{'fixture_id': fixture,
            'quote_id': quote, 'evidence_text': '全场大2.5'}])
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(Decimal('1.95'), Decimal(response.json()['accepted'][0]['price']))
        self.assertEqual(0, await self.owner_pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_account_admin_orders_and_state_writes_are_forbidden(self):
        for sql in ('SELECT * FROM public.v2_admin_settings', 'SELECT * FROM v2_users',
                    'SELECT * FROM v2_bet_orders', 'SELECT * FROM v2_leyu_accounts', 'SELECT * FROM v2_user_bet_settings',
                    "UPDATE v2_analysis_jobs SET state='failed'", "UPDATE v2_fixtures SET phase='finished'",
                    'DELETE FROM v2_expert_signals', 'CREATE TABLE public.forbidden(id int)'):
            with self.subTest(sql=sql), self.assertRaises(asyncpg.InsufficientPrivilegeError):
                await self.restricted.execute(sql)

    async def test_verification_rejects_extra_column_access(self):
        async with self.owner_pool.acquire() as conn:
            await conn.execute('GRANT SELECT(email) ON v2_users TO "' + self.role + '"')
            with self.assertRaises(ProvisionError) as caught:
                await verify(conn, self.role)
            self.assertEqual('mcp_column_privileges_invalid', str(caught.exception))

    async def test_temp_table_cannot_shadow_validated_quote_policy_view(self):
        async with self.restricted.acquire() as conn:
            await conn.execute('CREATE TEMP TABLE v2_admin_settings(setting_key text,encrypted_value text)')
            await conn.execute("INSERT INTO pg_temp.v2_admin_settings VALUES('quote_max_age_seconds','mcp-quote-policy-v1:3600')")
            self.assertEqual([], await conn.fetch('SELECT * FROM v2_admin_settings'))

    async def test_projection_hot_changes_and_stale_cipher_fail_closed(self):
        settings = SimpleNamespace(encryption_key=b'k'*32)
        async with self.owner_pool.acquire() as conn:
            for seconds in (15, 90):
                cipher = encrypt_secret(settings.encryption_key, str(seconds))
                async with conn.transaction():
                    await conn.execute("INSERT INTO v2_admin_settings(setting_key,encrypted_value) "
                        "VALUES('quote_max_age_seconds',$1) ON CONFLICT(setting_key) "
                        'DO UPDATE SET encrypted_value=EXCLUDED.encrypted_value', cipher)
                    await sync_quote_policy(conn, settings)
                rows = await self.restricted.fetch('SELECT * FROM v2_admin_settings')
                self.assertEqual(1, len(rows))
                self.assertNotIn(cipher, str(rows))
                self.assertEqual(seconds, quote_max_age_from_rows(self.settings, rows))
            await conn.execute("UPDATE v2_admin_settings SET encrypted_value=$1 WHERE setting_key='quote_max_age_seconds'",
                               encrypt_secret(settings.encryption_key, '20'))
        with self.assertRaises(HTTPException) as caught:
            quote_max_age_from_rows(self.settings, await self.restricted.fetch('SELECT * FROM v2_admin_settings'))
        self.assertEqual(503, caught.exception.status_code)

    async def test_non_tool_routes_absent_and_failed_lease_writes_no_signals(self):
        database.pool = self.restricted
        for path in ('/api/v1/admin/settings', '/api/v1/auth/login', '/internal/executor/orders',
                     '/internal/review/config', '/docs', '/openapi.json'):
            self.assertEqual(404, (await self.client.get(path)).status_code)
        bad = await self.client.post('/internal/mcp/search_matches', json=self.context(team_query='Alpha'),
                                     headers={'Authorization': 'Bearer wrong'})
        self.assertEqual(401, bad.status_code)
        await self.owner_pool.execute("UPDATE v2_analysis_jobs SET lease_until=now()-interval '1 second'")
        self.assertEqual(409, (await self.post('search_matches', team_query='Alpha')).status_code)
        self.assertEqual(0, await self.owner_pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_continuation_survives_app_restart_and_cross_attempt_is_rejected(self):
        await self.fixture(1)
        await self.fixture(2)
        database.pool = self.restricted
        first = await self.post('search_matches', team_query='Alpha', page_size=1)
        self.assertEqual(200, first.status_code, first.text)
        cursor = first.json()['next_cursor']
        self.assertTrue(cursor)
        premature = await self.post('resolve_match', search_key=first.json()['search_key'],
                                    team_a='Alpha1', team_b='Beta1')
        self.assertEqual(409, premature.status_code)
        await self.client.aclose()
        replacement = mcp_app.create_app()
        replacement.state.settings = self.settings
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=replacement), base_url='http://test',
                                       headers={'Authorization': 'Bearer isolated-tools'})
        second = await self.post('search_matches', team_query='Alpha', page_size=1, cursor=cursor)
        self.assertEqual(200, second.status_code, second.text)
        self.assertTrue(second.json()['search_complete'])
        self.assertIsNone(second.json()['next_cursor'])
        crossed = await self.client.post('/internal/mcp/search_matches', json={
            **self.context(team_query='Alpha', cursor=cursor), 'attempt_id': str(uuid.uuid4())})
        self.assertEqual(409, crossed.status_code)
        self.assertEqual(0, await self.owner_pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_closed_quote_and_deactivated_source_cannot_record(self):
        fixture = await self.fixture()
        database.pool = self.restricted
        key, _ = await self.search_all()
        await self.resolve(key)
        listed = await self.post('list_match_markets', fixture_id=fixture, period='full_time',
                                 market_kind='total', line='2.5', selection='over')
        self.assertEqual(200, listed.status_code, listed.text)
        quote = listed.json()['markets'][0]['quote_id']
        await self.owner_pool.execute("UPDATE v2_current_quotes SET status='closed'")
        recorded = await self.post('record_expert_signals', signals=[{'fixture_id': fixture,
            'quote_id': quote, 'evidence_text': '全场大2.5'}])
        self.assertEqual(200, recorded.status_code, recorded.text)
        self.assertEqual([], recorded.json()['accepted'])
        await self.owner_pool.execute('UPDATE v2_expert_sources SET enabled=false')
        self.assertEqual(409, (await self.post('list_match_markets', fixture_id=fixture)).status_code)
        self.assertEqual(0, await self.owner_pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_missing_projection_is_not_ready_and_never_exposes_cipher(self):
        cipher = encrypt_secret(b'k'*32, '30')
        await self.owner_pool.execute("INSERT INTO v2_admin_settings(setting_key,encrypted_value) "
                                     "VALUES('quote_max_age_seconds',$1)", cipher)
        database.pool = self.restricted
        response = await self.client.get('/readyz')
        self.assertEqual(503, response.status_code, response.text)
        self.assertNotIn(cipher, response.text)
