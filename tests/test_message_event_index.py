"""Latest source evidence stays exact while unrelated history avoids scans."""
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
class MessageEventIndexTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_migrations.MigrationTests.asyncSetUp
    asyncTearDown = test_migrations.MigrationTests.asyncTearDown

    async def prior_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            for path in MIGRATIONS.glob('*.sql'):
                if path.name < '027':
                    Path(folder, path.name).write_bytes(path.read_bytes())
            await apply_migrations(self.conn, Path(folder))

    async def test_exact_concurrent_prebuild_registers_without_rebuild(self):
        await self.prior_schema()
        await self.conn.execute('CREATE INDEX CONCURRENTLY v2_telegram_events_message_latest_idx '
                                'ON v2_telegram_events(message_id,received_at DESC,id DESC)')
        before = await self.conn.fetchrow("SELECT oid,relfilenode FROM pg_class WHERE oid='v2_telegram_events_message_latest_idx'::regclass")
        self.assertEqual(['027_telegram_message_event_lookup.sql','028_quote_summary_lookup.sql','029_review_latest_result_lookup.sql'], await apply_migrations(self.conn))
        self.assertEqual(before, await self.conn.fetchrow("SELECT oid,relfilenode FROM pg_class WHERE oid='v2_telegram_events_message_latest_idx'::regclass"))
        self.assertEqual([], await apply_migrations(self.conn))
        await check_schema(self.conn)

    async def test_wrong_prebuild_refuses_version_registration(self):
        await self.prior_schema()
        await self.conn.execute('CREATE INDEX v2_telegram_events_message_latest_idx ON v2_telegram_events(message_id)')
        with self.assertRaisesRegex(asyncpg.RaiseError, 'definition or validity mismatch'):
            await apply_migrations(self.conn)
        self.assertFalse(await self.conn.fetchval("SELECT EXISTS(SELECT 1 FROM v2_schema_migrations WHERE version='027_telegram_message_event_lookup.sql')"))

    async def test_upgrade_preserves_all_evidence_and_latest_tie_order_without_scan(self):
        await self.prior_schema()
        source, expert, message, other = [uuid.uuid4() for _ in range(4)]
        await self.conn.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'chat','Source')", source)
        await self.conn.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'author','Expert')", expert)
        for identity in (message, other):
            await self.conn.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,$4,'Original',now())", identity, source, expert, str(identity))
        await self.conn.execute("""INSERT INTO v2_telegram_events(id,source_id,platform_message_id,version_key,
            source_version_at,event_kind,content,media_state,message_id,is_current,received_at)
            SELECT md5(i::text)::uuid,$1,i::text,'v1',now(),'message',
              jsonb_build_object('body','complete-'||i::text),'ready',
              CASE WHEN i<=3 THEN $2::uuid WHEN i=4 THEN NULL ELSE $3::uuid END,false,
              '2026-11-01T12:00:00Z'::timestamptz + CASE WHEN i=1 THEN interval '-1 second' ELSE interval '0' END
            FROM generate_series(1,12000) i""", source, message, other)
        query = 'SELECT id,last_error,content FROM v2_telegram_events WHERE message_id=$1 ORDER BY received_at DESC,id DESC LIMIT 1'
        expected = await self.conn.fetchrow(query, message)
        rows_before = await self.conn.fetch('SELECT *,xmin::text AS row_version,ctid::text AS location FROM v2_telegram_events ORDER BY id')
        self.assertEqual(['027_telegram_message_event_lookup.sql','028_quote_summary_lookup.sql','029_review_latest_result_lookup.sql'], await apply_migrations(self.conn))
        self.assertEqual(rows_before, await self.conn.fetch('SELECT *,xmin::text AS row_version,ctid::text AS location FROM v2_telegram_events ORDER BY id'))
        self.assertEqual(expected, await self.conn.fetchrow(query, message))
        self.assertIsNone(await self.conn.fetchrow(query, uuid.uuid4()))
        self.assertEqual(1, await self.conn.fetchval('SELECT count(*) FROM v2_telegram_events WHERE message_id IS NULL'))
        await self.conn.execute('ANALYZE v2_telegram_events')
        plan = json.loads(await self.conn.fetchval('EXPLAIN (FORMAT JSON) '+query, message))
        def nodes(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from nodes(child)
            elif isinstance(value, list):
                for child in value:
                    yield from nodes(child)
        items = list(nodes(plan))
        self.assertTrue(any(r.get('Index Name')=='v2_telegram_events_message_latest_idx' for r in items))
        self.assertFalse(any(r.get('Node Type') in {'Seq Scan','Sort'} for r in items))
        await check_schema(self.conn)
