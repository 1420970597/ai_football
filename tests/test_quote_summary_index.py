"""Bounded summary scans accept exact online prebuilds and preserve quotes."""
import json
import os
from pathlib import Path
import tempfile
import unittest

import asyncpg

from app.database import MIGRATIONS, apply_migrations, check_schema
from tests import test_migrations


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class QuoteSummaryIndexTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_migrations.MigrationTests.asyncSetUp
    asyncTearDown = test_migrations.MigrationTests.asyncTearDown

    async def prior_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            for path in MIGRATIONS.glob('*.sql'):
                if path.name < '028':
                    Path(folder, path.name).write_bytes(path.read_bytes())
            await apply_migrations(self.conn, Path(folder))

    async def test_exact_concurrent_prebuild_is_registered_without_rebuilding(self):
        await self.prior_schema()
        await self.conn.execute('CREATE INDEX CONCURRENTLY v2_quotes_fixture_summary_idx ON '
                                'v2_current_quotes(fixture_id,is_main DESC,source_updated_at DESC NULLS LAST,'
                                'market_id,line,selection)')
        before = await self.conn.fetchrow("SELECT oid,relfilenode FROM pg_class WHERE oid='v2_quotes_fixture_summary_idx'::regclass")
        self.assertEqual(['028_quote_summary_lookup.sql', '029_review_latest_result_lookup.sql'], await apply_migrations(self.conn))
        self.assertEqual(before, await self.conn.fetchrow(
            "SELECT oid,relfilenode FROM pg_class WHERE oid='v2_quotes_fixture_summary_idx'::regclass"))
        self.assertEqual([], await apply_migrations(self.conn))
        await check_schema(self.conn)

    async def test_wrong_null_order_refuses_migration_and_preserves_prebuild(self):
        await self.prior_schema()
        await self.conn.execute('CREATE INDEX v2_quotes_fixture_summary_idx ON '
                                'v2_current_quotes(fixture_id,is_main DESC,source_updated_at DESC,'
                                'market_id,line,selection)')
        definition = await self.conn.fetchval("SELECT pg_get_indexdef('v2_quotes_fixture_summary_idx'::regclass)")
        with self.assertRaisesRegex(asyncpg.RaiseError, 'definition or validity mismatch'):
            await apply_migrations(self.conn)
        self.assertFalse(await self.conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM v2_schema_migrations WHERE version='028_quote_summary_lookup.sql')"))
        self.assertEqual(definition, await self.conn.fetchval(
            "SELECT pg_get_indexdef('v2_quotes_fixture_summary_idx'::regclass)"))

    async def test_index_preserves_quotes_and_limits_each_heap_scan_without_inner_sort(self):
        await self.prior_schema()
        await self.conn.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) "
                                "SELECT 'fixture-'||i,'source-'||i,'football','League','Home','Away',now(),'live' "
                                "FROM generate_series(1,20) i")
        await self.conn.execute(
            "INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,period,market_kind,line,"
            "selection,price,source_updated_at,status,is_main) "
            "SELECT md5(f||'-'||q)::uuid,'fixture-'||f,'market-'||q,'Market','full_time','total',q/4.0,"
            "'over',2,CASE WHEN q%5=0 THEN NULL ELSE now()-q*interval '1 second' END,"
            "CASE WHEN q%3=0 THEN 'closed' ELSE 'active' END,q%6=0 "
            "FROM generate_series(1,20) f CROSS JOIN generate_series(1,1200) q")
        before = await self.conn.fetch('SELECT *,xmin::text AS row_version,ctid::text AS location FROM v2_current_quotes ORDER BY id')
        query = ("SELECT q.* FROM unnest($1::text[]) AS wanted(fixture_id) CROSS JOIN LATERAL ("
                 "SELECT id,fixture_id,market_id,line,selection,price,status,is_main,source_updated_at "
                 "FROM v2_current_quotes WHERE fixture_id=wanted.fixture_id "
                 "ORDER BY is_main DESC,source_updated_at DESC NULLS LAST,market_id,line,selection LIMIT 24) q "
                 "ORDER BY q.fixture_id,q.market_id,q.line,q.selection")
        selected = ['fixture-1', 'fixture-2', 'fixture-20', 'missing']
        expected = await self.conn.fetch(query, selected)
        self.assertEqual(72, len(expected))
        self.assertEqual(['028_quote_summary_lookup.sql', '029_review_latest_result_lookup.sql'], await apply_migrations(self.conn))
        self.assertEqual(before, await self.conn.fetch('SELECT *,xmin::text AS row_version,ctid::text AS location FROM v2_current_quotes ORDER BY id'))
        self.assertEqual(expected, await self.conn.fetch(query, selected))
        await self.conn.execute('ANALYZE v2_current_quotes')
        plan = json.loads(await self.conn.fetchval('EXPLAIN(ANALYZE,FORMAT JSON) '+query, selected))

        def nodes(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from nodes(child)
            elif isinstance(value, list):
                for child in value:
                    yield from nodes(child)

        indexes = [node for node in nodes(plan) if node.get('Index Name') == 'v2_quotes_fixture_summary_idx']
        self.assertEqual(1, len(indexes))
        self.assertLessEqual(indexes[0]['Actual Rows'], 24)
        self.assertEqual(len(selected), indexes[0]['Actual Loops'])
        # The final display sort is expected; the per-fixture branch must be
        # Limit -> Index Scan without a sort or full bitmap/sequence scan.
        limits = [node for node in nodes(plan) if node.get('Node Type') == 'Limit']
        self.assertEqual(1, len(limits))
        self.assertEqual('Index Scan', limits[0]['Plans'][0]['Node Type'])
        self.assertEqual([], await apply_migrations(self.conn))
        await check_schema(self.conn)
