"""Nonclosed lookup preserves rows and avoids historical closure scans."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import asyncpg

from app.database import MIGRATIONS, apply_migrations, check_schema
from tests import test_migrations


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class QuoteClosureIndexTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_migrations.MigrationTests.asyncSetUp
    asyncTearDown = test_migrations.MigrationTests.asyncTearDown

    async def prior_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            prior = Path(folder)
            for path in MIGRATIONS.glob('*.sql'):
                if path.name < '026':
                    (prior / path.name).write_bytes(path.read_bytes())
            await apply_migrations(self.conn, prior)

    async def test_matching_concurrent_prebuild_is_accepted_without_rebuilding(self):
        await self.prior_schema()
        await self.conn.execute("CREATE INDEX CONCURRENTLY v2_quotes_nonclosed_fixture_idx ON v2_current_quotes(fixture_id) WHERE status <> 'closed'")
        before = await self.conn.fetchrow("SELECT oid,relfilenode FROM pg_class WHERE oid='v2_quotes_nonclosed_fixture_idx'::regclass")
        self.assertEqual(['026_quote_nonclosed_lookup.sql','027_telegram_message_event_lookup.sql','028_quote_summary_lookup.sql'], await apply_migrations(self.conn))
        self.assertEqual(before, await self.conn.fetchrow("SELECT oid,relfilenode FROM pg_class WHERE oid='v2_quotes_nonclosed_fixture_idx'::regclass"))
        await check_schema(self.conn)

    async def test_wrong_prebuilt_index_is_rejected_and_migration_not_recorded(self):
        await self.prior_schema()
        await self.conn.execute("CREATE INDEX v2_quotes_nonclosed_fixture_idx ON v2_current_quotes(fixture_id) WHERE status = 'active'")
        before = await self.conn.fetchval("SELECT pg_get_indexdef('v2_quotes_nonclosed_fixture_idx'::regclass)")
        with self.assertRaisesRegex(asyncpg.RaiseError, 'definition or validity mismatch'):
            await apply_migrations(self.conn)
        self.assertEqual(before, await self.conn.fetchval("SELECT pg_get_indexdef('v2_quotes_nonclosed_fixture_idx'::regclass)"))
        self.assertFalse(await self.conn.fetchval("SELECT EXISTS(SELECT 1 FROM v2_schema_migrations WHERE version='026_quote_nonclosed_lookup.sql')"))

    async def test_upgrade_preserves_rows_and_indexes_active_and_suspended_only(self):
        with tempfile.TemporaryDirectory() as folder:
            prior = Path(folder)
            for path in MIGRATIONS.glob('*.sql'):
                if path.name < '026':
                    (prior / path.name).write_bytes(path.read_bytes())
            await apply_migrations(self.conn, prior)
        await self.conn.execute(
            "INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) "
            "VALUES('fixture','source','football','League','Home','Away',now(),'live')")
        await self.conn.execute(
            "INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,market_kind,period,line,"
            "selection,price,source_updated_at,status) "
            "SELECT md5(i::text)::uuid,'fixture',i::text,'Market','handicap','full_time',0,'home',2,now(),"
            "CASE WHEN i=1 THEN 'active' WHEN i=2 THEN 'suspended' ELSE 'closed' END "
            "FROM generate_series(1,12000) i")
        before = await self.conn.fetch('SELECT *,xmin::text AS row_version,ctid::text AS location FROM v2_current_quotes ORDER BY id')
        self.assertEqual(['026_quote_nonclosed_lookup.sql','027_telegram_message_event_lookup.sql','028_quote_summary_lookup.sql'], await apply_migrations(self.conn))
        self.assertEqual(before, await self.conn.fetch('SELECT *,xmin::text AS row_version,ctid::text AS location FROM v2_current_quotes ORDER BY id'))
        self.assertEqual([], await apply_migrations(self.conn))
        await check_schema(self.conn)
        await self.conn.execute('ANALYZE v2_current_quotes')
        statement = "UPDATE v2_current_quotes SET status='closed' WHERE fixture_id=ANY($1::text[]) AND status<>'closed'"
        plan = json.loads(await self.conn.fetchval('EXPLAIN (FORMAT JSON) ' + statement, ['fixture']))
        def indexes(value):
            if isinstance(value, dict):
                return [value['Index Name']] if 'Index Name' in value else [name for child in value.values() for name in indexes(child)]
            if isinstance(value, list):
                return [name for child in value for name in indexes(child)]
            return []
        self.assertIn('v2_quotes_nonclosed_fixture_idx', indexes(plan))
        self.assertEqual('UPDATE 2', await self.conn.execute(statement, ['fixture']))
        self.assertEqual('UPDATE 0', await self.conn.execute(statement, ['fixture']))
        closed_before = {r['id']:r for r in before if r['status']=='closed'}
        after = await self.conn.fetch('SELECT *,xmin::text AS row_version,ctid::text AS location FROM v2_current_quotes ORDER BY id')
        self.assertTrue(all(r['status']=='closed' for r in after))
        self.assertEqual(closed_before, {r['id']:r for r in after if r['id'] in closed_before})
