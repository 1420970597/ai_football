import asyncio
import os
from pathlib import Path
import tempfile
import unittest
import uuid

import asyncpg

from app.database import MIGRATIONS, MigrationRequired, apply_migrations, check_schema


@unittest.skipUnless(os.environ.get("TEST_DATABASE_ADMIN_URL"), "requires isolated database admin URL")
class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.admin_url = os.environ["TEST_DATABASE_ADMIN_URL"]
        self.database = "test_migrations_" + uuid.uuid4().hex
        self.admin = await asyncpg.connect(self.admin_url)
        await self.admin.execute(f'CREATE DATABASE "{self.database}"')
        self.url = self.admin_url.rsplit("/", 1)[0] + "/" + self.database
        self.conn = await asyncpg.connect(self.url)

    async def asyncTearDown(self):
        await self.conn.close()
        await self.admin.execute(f'DROP DATABASE "{self.database}" WITH (FORCE)')
        await self.admin.close()

    async def test_fresh_database_requires_explicit_migration(self):
        with self.assertRaises(MigrationRequired):
            await check_schema(self.conn)
        versions = await apply_migrations(self.conn)
        self.assertTrue(versions)
        await check_schema(self.conn)
        self.assertEqual([], await apply_migrations(self.conn))

    async def test_concurrent_cold_start_is_serialized(self):
        other = await asyncpg.connect(self.url)
        try:
            first, second = await asyncio.gather(apply_migrations(self.conn), apply_migrations(other))
            self.assertEqual(1, sum(bool(v) for v in (first, second)))
            await check_schema(self.conn)
            count = await self.conn.fetchval("SELECT count(*) FROM v2_schema_migrations")
            self.assertEqual(len(first or second), count)
        finally:
            await other.close()

    async def test_all_phase_search_upgrade_preserves_existing_searches_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as folder:
            prior = Path(folder)
            for path in MIGRATIONS.glob('*.sql'):
                if path.name < '021':
                    (prior / path.name).write_bytes(path.read_bytes())
            await apply_migrations(self.conn, prior)
        source, expert, message, job = [uuid.uuid4() for _ in range(4)]
        await self.conn.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'chat','Source')", source)
        await self.conn.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'author','Expert')", expert)
        await self.conn.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,'1','Original',now())", message, source, expert)
        await self.conn.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key) VALUES($1,$2,'queued',$3)", job, message, str(job))
        await self.conn.execute("INSERT INTO v2_match_searches(job_id,search_key,team_query,phase) VALUES($1,'original','Team','all')", job)
        before = dict(await self.conn.fetchrow('SELECT * FROM v2_match_searches WHERE job_id=$1', job))
        self.assertEqual(['021_review_search_all_phases.sql'], await apply_migrations(self.conn))
        self.assertEqual(before, dict(await self.conn.fetchrow('SELECT * FROM v2_match_searches WHERE job_id=$1', job)))
        for phase in ('finished', 'cancelled'):
            await self.conn.execute('INSERT INTO v2_match_searches(job_id,search_key,team_query,phase) VALUES($1,$2,$3,$2)', job, phase, 'Team')
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.conn.execute("INSERT INTO v2_match_searches(job_id,search_key,team_query,phase) VALUES($1,'invalid','Team','unknown')", job)
        self.assertEqual([], await apply_migrations(self.conn))
        await check_schema(self.conn)

    async def test_changed_migration_and_unknown_version_rejected(self):
        await apply_migrations(self.conn)
        await self.conn.execute("UPDATE v2_schema_migrations SET checksum='tampered'")
        with self.assertRaises(MigrationRequired):
            await apply_migrations(self.conn)
        with self.assertRaises(MigrationRequired):
            await check_schema(self.conn)

    async def test_failed_batch_rolls_back_and_can_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "001_first.sql").write_text("CREATE TABLE v2_test_record (id int);")
            (directory / "002_error.sql").write_text("INSERT INTO nonexistent_table VALUES(1);")
            with self.assertRaises(asyncpg.UndefinedTableError):
                await apply_migrations(self.conn, directory)
            self.assertIsNone(await self.conn.fetchval("SELECT to_regclass('v2_test_record')"))
            (directory / "002_error.sql").write_text("INSERT INTO v2_test_record VALUES(1);")
            self.assertEqual(2, len(await apply_migrations(self.conn, directory)))
            self.assertEqual(1, await self.conn.fetchval("SELECT id FROM v2_test_record"))

    async def test_normal_schema_verification_performs_no_ddl(self):
        await apply_migrations(self.conn)
        role = "test_reader_" + uuid.uuid4().hex
        await self.conn.execute(f'CREATE ROLE "{role}"')
        try:
            await self.conn.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
            await self.conn.execute(f'GRANT SELECT ON v2_schema_migrations TO "{role}"')
            async with self.conn.transaction():
                await self.conn.execute(f'SET LOCAL ROLE "{role}"')
                await check_schema(self.conn)
                with self.assertRaises(asyncpg.InsufficientPrivilegeError):
                    await self.conn.execute("CREATE TABLE v2_should_not_exist (id int)")
        finally:
            await self.conn.execute(f'DROP OWNED BY "{role}"')
            await self.conn.execute(f'DROP ROLE "{role}"')
