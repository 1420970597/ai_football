"""035 drops only the redundant active-quote index and keeps its covering index."""
import os
from pathlib import Path
import tempfile
import unittest

import asyncpg

from app.database import MIGRATIONS, apply_migrations, check_schema
from tests import test_migrations

VERSION = '035_drop_redundant_active_quote_index.sql'
INDEXES = ("SELECT indexname FROM pg_indexes WHERE tablename='v2_current_quotes' "
           "AND indexname=ANY($1::text[]) ORDER BY indexname")
BOTH = ['v2_current_quotes_active_idx', 'v2_current_quotes_fixture_fresh_idx']
ROWS = 'SELECT *,xmin::text AS row_version,ctid::text AS location FROM v2_current_quotes ORDER BY id'


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class QuoteActiveIndexDropTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_migrations.MigrationTests.asyncSetUp
    asyncTearDown = test_migrations.MigrationTests.asyncTearDown

    async def prior_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            for path in MIGRATIONS.glob('*.sql'):
                if path.name < '035':
                    Path(folder, path.name).write_bytes(path.read_bytes())
            await apply_migrations(self.conn, Path(folder))

    async def indexes(self):
        return [row['indexname'] for row in await self.conn.fetch(INDEXES, BOTH)]

    async def recorded(self):
        return await self.conn.fetchval(
            'SELECT EXISTS(SELECT 1 FROM v2_schema_migrations WHERE version=$1)', VERSION)

    async def test_upgrade_drops_only_the_redundant_index_and_preserves_quotes(self):
        await self.prior_schema()
        self.assertEqual(BOTH, await self.indexes())
        await self.conn.execute(
            "INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) "
            "VALUES('fixture','source','football','League','Home','Away',now(),'live')")
        await self.conn.execute(
            "INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,market_kind,period,line,"
            "selection,price,source_updated_at,status) "
            "SELECT md5(i::text)::uuid,'fixture',i::text,'Market','handicap','full_time',0,'home',2,"
            "now()-i*interval '1 second',CASE WHEN i%3=0 THEN 'closed' ELSE 'active' END "
            "FROM generate_series(1,300) i")
        others = ("SELECT indexname,indexdef FROM pg_indexes WHERE tablename='v2_current_quotes' "
                  "AND indexname<>'v2_current_quotes_active_idx' ORDER BY indexname")
        before, kept = await self.conn.fetch(ROWS), await self.conn.fetch(others)
        self.assertEqual(test_migrations.migration_suffix(VERSION), await apply_migrations(self.conn))
        self.assertEqual(['v2_current_quotes_fixture_fresh_idx'], await self.indexes())
        self.assertEqual(kept, await self.conn.fetch(others))
        self.assertEqual(before, await self.conn.fetch(ROWS))
        self.assertEqual([], await apply_migrations(self.conn))
        await check_schema(self.conn)

    async def test_concurrent_predrop_is_registered(self):
        await self.prior_schema()
        await self.conn.execute('DROP INDEX CONCURRENTLY IF EXISTS v2_current_quotes_active_idx')
        self.assertEqual(test_migrations.migration_suffix(VERSION), await apply_migrations(self.conn))
        self.assertTrue(await self.recorded())
        await check_schema(self.conn)

    async def test_missing_covering_index_refuses_and_keeps_the_active_index(self):
        await self.prior_schema()
        await self.conn.execute('DROP INDEX v2_current_quotes_fixture_fresh_idx')
        with self.assertRaisesRegex(asyncpg.RaiseError, 'covering fixture quote index'):
            await apply_migrations(self.conn)
        self.assertEqual(['v2_current_quotes_active_idx'], await self.indexes())
        self.assertFalse(await self.recorded())

    async def test_narrower_covering_index_refuses(self):
        await self.prior_schema()
        await self.conn.execute('DROP INDEX v2_current_quotes_fixture_fresh_idx')
        await self.conn.execute("CREATE INDEX v2_current_quotes_fixture_fresh_idx ON "
                                "v2_current_quotes(fixture_id,source_updated_at DESC) WHERE status<>'closed'")
        with self.assertRaisesRegex(asyncpg.RaiseError, 'covering fixture quote index'):
            await apply_migrations(self.conn)
        self.assertEqual(BOTH, await self.indexes())
        self.assertFalse(await self.recorded())
