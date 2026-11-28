"""Latest framework health evidence stays exact with a narrow ordered index."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

import asyncpg

from app.database import MIGRATIONS, apply_migrations, check_schema
from tests import test_migrations


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class ReviewLatestResultIndexTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_migrations.MigrationTests.asyncSetUp
    asyncTearDown = test_migrations.MigrationTests.asyncTearDown

    async def prior_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            for path in MIGRATIONS.glob('*.sql'):
                if path.name < '029':
                    Path(folder, path.name).write_bytes(path.read_bytes())
            await apply_migrations(self.conn, Path(folder))

    async def test_exact_concurrent_prebuild_is_registered_without_rebuilding(self):
        await self.prior_schema()
        await self.conn.execute(
            "CREATE INDEX CONCURRENTLY v2_review_latest_result_idx ON v2_analysis_traces(created_at DESC) "
            "WHERE trace_kind='hermes_result'")
        query = "SELECT oid,relfilenode FROM pg_class WHERE oid='v2_review_latest_result_idx'::regclass"
        before = await self.conn.fetchrow(query)
        other = await asyncpg.connect(self.url)
        try:
            results = await asyncio.gather(apply_migrations(self.conn), apply_migrations(other))
        finally:
            await other.close()
        self.assertEqual(1, sum(bool(v) for v in results))
        self.assertIn(test_migrations.migration_suffix('029_review_latest_result_lookup.sql'), results)
        self.assertEqual(before, await self.conn.fetchrow(query))
        self.assertEqual([], await apply_migrations(self.conn))
        await check_schema(self.conn)

    async def test_wrong_same_name_predicate_is_rejected_without_registration(self):
        await self.prior_schema()
        await self.conn.execute(
            "CREATE INDEX v2_review_latest_result_idx ON v2_analysis_traces(created_at DESC) "
            "WHERE trace_kind='system_review_result'")
        query = "SELECT pg_get_indexdef('v2_review_latest_result_idx'::regclass)"
        before = await self.conn.fetchval(query)
        with self.assertRaisesRegex(asyncpg.RaiseError, 'definition or validity mismatch'):
            await apply_migrations(self.conn)
        self.assertEqual(before, await self.conn.fetchval(query))
        self.assertFalse(await self.conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM v2_schema_migrations WHERE version='029_review_latest_result_lookup.sql')"))

    async def test_upgrade_preserves_traces_and_latest_evidence_uses_ordered_partial_index(self):
        await self.prior_schema()
        source, expert, message, job = [uuid.uuid4() for _ in range(4)]
        await self.conn.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'isolated','Source')", source)
        await self.conn.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'isolated','Expert')", expert)
        await self.conn.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) "
                                "VALUES($1,$2,$3,'1','Isolated source',now())", message, source, expert)
        await self.conn.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key) VALUES($1,$2,'unresolved','isolated')", job, message)
        await self.conn.execute(
            "INSERT INTO v2_analysis_traces(id,job_id,sequence_no,trace_kind,payload,created_at) "
            "SELECT md5('trace-'||i)::uuid,$1,i,CASE WHEN i%10=0 THEN 'hermes_result' ELSE 'tool_result' END,"
            "jsonb_build_object('model','sample-model-'||i),now()-i*interval '1 second' FROM generate_series(1,6000) i", job)
        before = await self.conn.fetch(
            'SELECT *,xmin::text AS row_version,ctid::text AS location FROM v2_analysis_traces ORDER BY sequence_no')
        query = "SELECT created_at,payload->>'model' AS model FROM v2_analysis_traces WHERE trace_kind='hermes_result' ORDER BY created_at DESC LIMIT 1"
        expected = await self.conn.fetchrow(query)
        self.assertEqual('sample-model-10', expected['model'])
        self.assertEqual(test_migrations.migration_suffix('029_review_latest_result_lookup.sql'), await apply_migrations(self.conn))
        self.assertEqual(before, await self.conn.fetch(
            'SELECT *,xmin::text AS row_version,ctid::text AS location FROM v2_analysis_traces ORDER BY sequence_no'))
        self.assertEqual(expected, await self.conn.fetchrow(query))
        await self.conn.execute('ANALYZE v2_analysis_traces')
        plan = json.loads(await self.conn.fetchval('EXPLAIN(ANALYZE,FORMAT JSON) '+query))[0]['Plan']
        self.assertEqual('Limit', plan['Node Type'])
        child = plan['Plans'][0]
        self.assertEqual('Index Scan', child['Node Type'])
        self.assertEqual('v2_review_latest_result_idx', child['Index Name'])
        self.assertEqual(1, child['Actual Rows'])
        self.assertEqual([], await apply_migrations(self.conn))
        await check_schema(self.conn)
