import asyncio
import io
import json
import os
from urllib.parse import urlsplit
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest
import uuid

import asyncpg

import test_reviews as support
from app import worker
from app.database import apply_migrations, migration_files


@unittest.skipUnless(os.environ.get("TEST_DATABASE_ADMIN_URL"), "requires isolated database")
class ReviewWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parts = urlsplit(os.environ['TEST_DATABASE_ADMIN_URL'])
        if parts.hostname not in {'ai-football-v2-pgtest', 'ai-football-v2-pgverify'} or parts.path != '/postgres':
            raise RuntimeError('worker tests require an explicit task Docker database')
        await support.ReviewTests.asyncSetUp(self)

    asyncTearDown = support.ReviewTests.asyncTearDown

    async def add_expert(self, source=None, expert=None):
        source, expert = source or uuid.uuid4(), expert or uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,$2,'Synthetic source') ON CONFLICT DO NOTHING", source, str(source))
        await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,$2,'Synthetic expert') ON CONFLICT DO NOTHING", expert, str(expert))
        await self.pool.execute('INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2) ON CONFLICT DO NOTHING', source, expert)
        return source, expert

    async def queued(self, source=None, expert=None, count=1, old=False):
        source, expert = source or self.source, expert or self.expert
        ids = []
        for _ in range(count):
            message, job = uuid.uuid4(), uuid.uuid4()
            await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,$4,'SYNTHETIC scheduler test',now())", message, source, expert, str(message))
            await self.pool.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,available_at) VALUES($1,$2,'queued',$3,now()-$4::int*interval '1 day')", job, message, str(job), int(old))
            ids.append(job)
        return ids

    async def complete(self, job):
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='unresolved',lease_until=NULL WHERE id=$1", job['id'])

    async def second_pool(self):
        async def codecs(conn):
            await conn.set_type_codec('jsonb', schema='pg_catalog', encoder=json.dumps, decoder=json.loads)
        return await asyncpg.create_pool(self.admin_url.rsplit('/', 1)[0]+'/'+self.dbname, min_size=1, max_size=3, init=codecs)

    async def make_queued(self, attempts=0):
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='queued',attempts=$2,attempt_id=NULL,lease_until=NULL WHERE id=$1", self.job, attempts)

    async def test_only_one_worker_claims_and_attempt_changes_after_expiry(self):
        await self.make_queued()
        first, second = await asyncio.gather(worker.claim_job(self.pool), worker.claim_job(self.pool))
        self.assertEqual(1, sum(item is not None for item in (first, second)))
        claimed = first or second
        await self.pool.execute("UPDATE v2_analysis_jobs SET lease_until=now()-interval '1 second' WHERE id=$1", self.job)
        retry = await worker.claim_job(self.pool)
        self.assertNotEqual(claimed["attempt_id"], retry["attempt_id"])
        self.assertEqual(2, retry["attempts"])

    async def test_final_attempt_crash_enters_dead_letter(self):
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='running',attempts=5,lease_until=now()-interval '1 second'")
        self.assertIsNone(await worker.claim_job(self.pool))
        row = await self.pool.fetchrow("SELECT state,last_error FROM v2_analysis_jobs WHERE id=$1", self.job)
        self.assertEqual("failed", row["state"])
        self.assertEqual("lease_exhausted", row["last_error"])

    async def test_incomplete_media_and_disabled_sources_are_not_claimed(self):
        await self.make_queued()
        await self.pool.execute("UPDATE v2_expert_messages SET media_state='pending'")
        self.assertIsNone(await worker.claim_job(self.pool))
        await self.pool.execute("UPDATE v2_expert_messages SET media_state='ready'")
        await self.pool.execute("UPDATE v2_expert_sources SET enabled=false")
        self.assertIsNone(await worker.claim_job(self.pool))

    async def test_model_text_without_recorded_signals_remains_unresolved(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        with patch.object(worker, "review_message", AsyncMock(return_value=("已记录盈利推荐", {"content": "已记录盈利推荐"}))), patch.object(worker, "delete_session", AsyncMock()):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model="hermes-agent"), job)
        self.assertEqual("unresolved", await self.pool.fetchval("SELECT state FROM v2_analysis_jobs WHERE id=$1", self.job))

    async def test_stale_worker_failure_cannot_overwrite_new_attempt(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        new_attempt = uuid.uuid4()
        await self.pool.execute("UPDATE v2_analysis_jobs SET attempt_id=$1 WHERE id=$2", new_attempt, self.job)
        with patch.object(worker, "delete_session", AsyncMock()):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model="hermes-agent"), job)
        row = await self.pool.fetchrow("SELECT state,attempt_id FROM v2_analysis_jobs WHERE id=$1", self.job)
        self.assertEqual("running", row["state"])
        self.assertEqual(new_attempt, row["attempt_id"])

    async def test_timeout_is_retryable_and_cleanup_runs(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        cleanup = AsyncMock()
        with patch.object(worker, "review_message", AsyncMock(side_effect=TimeoutError)), patch.object(worker, "delete_session", cleanup):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model="hermes-agent"), job)
        row = await self.pool.fetchrow("SELECT state,last_error,lease_until FROM v2_analysis_jobs WHERE id=$1", self.job)
        self.assertEqual("retry", row["state"])
        self.assertEqual("TimeoutError", row["last_error"])
        self.assertIsNone(row["lease_until"])
        trace_row = await self.pool.fetchrow(
            "SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='review_failure'",
            self.job,
        )
        self.assertEqual("timeout", trace_row["payload"]["failure_category"])
        self.assertTrue(trace_row["payload"]["retryable"])
        cleanup.assert_awaited_once()

    async def test_permanent_visual_failure_enters_dead_letter_without_retry(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        with patch.object(worker, "review_message", AsyncMock(side_effect=RuntimeError("unsupported Telegram image media type"))), patch.object(worker, "delete_session", AsyncMock()):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model="hermes-agent"), job)
        row = await self.pool.fetchrow("SELECT state,last_error FROM v2_analysis_jobs WHERE id=$1", self.job)
        self.assertEqual("failed", row["state"])
        self.assertEqual("RuntimeError", row["last_error"])
        failure = await self.pool.fetchrow("SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='review_failure'", self.job)
        self.assertEqual("visual_input_invalid", failure["payload"]["failure_category"])
        self.assertFalse(failure["payload"]["retryable"])

    async def test_heartbeat_stops_when_attempt_is_replaced(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        await self.pool.execute("UPDATE v2_analysis_jobs SET attempt_id=$1 WHERE id=$2", uuid.uuid4(), self.job)
        with patch.object(worker, "HEARTBEAT_SECONDS", 0.01):
            with self.assertRaises(worker.LeaseLost):
                await worker.keep_lease(self.pool, job)

    async def test_two_backlogged_experts_share_default_worker_concurrency(self):
        await self.make_queued()
        _, other = await self.add_expert(source=self.source)
        await self.queued(count=30, old=True)
        await self.queued(expert=other, count=5)
        first, second = await worker.claim_job(self.pool), await worker.claim_job(self.pool)
        self.assertEqual({self.expert, other}, {first['expert_id'], second['expert_id']})
        self.assertIsNone(await worker.claim_job(self.pool))
        self.assertEqual(2, await self.pool.fetchval("SELECT count(*) FROM v2_analysis_jobs WHERE state='running' AND lease_until>clock_timestamp()"))

    async def test_busy_source_does_not_starve_new_source(self):
        await self.make_queued()
        other_source, other_expert = await self.add_expert()
        await self.queued(count=40, old=True)
        await self.queued(source=other_source, expert=other_expert, count=5)
        seen = []
        for _ in range(8):
            job = await worker.claim_job(self.pool)
            seen.append(job['source_id'])
            await self.complete(job)
        for offset in (0, 2, 4, 6):
            self.assertEqual({self.source, other_source}, set(seen[offset:offset+2]))

    async def test_expert_turns_are_fair_inside_one_source(self):
        await self.make_queued()
        _, other = await self.add_expert(source=self.source)
        await self.queued(count=30, old=True)
        await self.queued(expert=other, count=5)
        seen = []
        for _ in range(8):
            job = await worker.claim_job(self.pool)
            seen.append(job['expert_id'])
            await self.complete(job)
        for offset in (0, 2, 4, 6):
            self.assertEqual({self.expert, other}, set(seen[offset:offset+2]))

    async def test_expert_budget_is_global_across_sources_and_independent_pools(self):
        await self.make_queued()
        other_source, _ = await self.add_expert(expert=self.expert)
        _, other_expert = await self.add_expert(source=other_source)
        await self.queued(count=5)
        await self.queued(source=other_source, expert=self.expert, count=5)
        await self.queued(source=other_source, expert=other_expert, count=5)
        second_pool = await self.second_pool()
        claimed = []
        try:
            for _ in range(4):
                values = await asyncio.gather(worker.claim_job(self.pool), worker.claim_job(second_pool), worker.claim_job(self.pool))
                claimed.extend(row for row in values if row)
            self.assertEqual(2, len(claimed))
            self.assertEqual({self.expert, other_expert}, {row['expert_id'] for row in claimed})
            rows = await self.pool.fetch("SELECT expert_id,count(*) AS n FROM v2_analysis_jobs WHERE state='running' AND lease_until>clock_timestamp() GROUP BY expert_id")
            self.assertTrue(all(row['n'] == 1 for row in rows))
        finally:
            await second_pool.close()

    async def test_database_budget_two_is_shared_and_lowering_drains_existing_leases(self):
        await self.make_queued()
        await self.queued(count=5)
        await self.pool.execute('INSERT INTO v2_review_expert_schedule(expert_id,concurrency_limit) VALUES($1,2) ON CONFLICT(expert_id) DO UPDATE SET concurrency_limit=2', self.expert)
        first, second = await worker.claim_job(self.pool), await worker.claim_job(self.pool)
        self.assertTrue(first and second)
        self.assertIsNone(await worker.claim_job(self.pool))
        await self.pool.execute('UPDATE v2_review_expert_schedule SET concurrency_limit=1 WHERE expert_id=$1', self.expert)
        await self.complete(first)
        self.assertIsNone(await worker.claim_job(self.pool))
        await self.complete(second)
        self.assertIsNotNone(await worker.claim_job(self.pool))

    async def test_expired_attempt_is_reclaimed_before_new_job_and_cannot_be_revived(self):
        await self.make_queued()
        old = await worker.claim_job(self.pool)
        await self.queued(count=3, old=True)
        await self.pool.execute("UPDATE v2_analysis_jobs SET lease_until=now()-interval '1 second' WHERE id=$1", old['id'])
        new = await worker.claim_job(self.pool)
        self.assertEqual(old['id'], new['id'])
        self.assertNotEqual(old['attempt_id'], new['attempt_id'])
        self.assertIs(False, await worker._renew_lease(self.pool, old))
        self.assertIsNone(await worker.claim_job(self.pool))

    async def test_skip_locked_head_uses_next_job_and_does_not_block_other_source(self):
        await self.make_queued()
        other = (await self.queued())[0]
        async with self.pool.acquire() as locker:
            async with locker.transaction():
                await locker.fetchrow('SELECT id FROM v2_analysis_jobs WHERE id=$1 FOR UPDATE', self.job)
                claimed = await worker.claim_job(self.pool)
                self.assertEqual(other, claimed['id'])
        await self.complete(claimed)
        other_source, other_expert = await self.add_expert()
        other_job = (await self.queued(source=other_source, expert=other_expert))[0]
        await self.pool.execute('UPDATE v2_review_source_schedule SET last_turn=0 WHERE source_id=$1', self.source)
        async with self.pool.acquire() as locker:
            async with locker.transaction():
                await locker.fetchrow('SELECT id FROM v2_analysis_jobs WHERE id=$1 FOR UPDATE', self.job)
                claimed = await worker.claim_job(self.pool)
                self.assertEqual(other_job, claimed['id'])

    async def test_scheduler_lock_contention_does_not_evict_valid_heartbeat(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        heartbeat = None
        with patch.object(worker, 'HEARTBEAT_SECONDS', .01):
            try:
                async with self.pool.acquire() as locker:
                    async with locker.transaction():
                        await locker.execute('SELECT pg_advisory_xact_lock(hashtext($1))', worker.SCHEDULER_LOCK)
                        heartbeat = asyncio.create_task(worker.keep_lease(self.pool, job))
                        await asyncio.sleep(.06)
                        self.assertFalse(heartbeat.done())
                        self.assertIsNone(await worker.claim_job(self.pool))
                for _ in range(30):
                    lease = await self.pool.fetchval('SELECT lease_until FROM v2_analysis_jobs WHERE id=$1', job['id'])
                    if lease > job['lease_until']:
                        break
                    await asyncio.sleep(.02)
                self.assertGreater(lease, job['lease_until'])
                self.assertFalse(heartbeat.done())
            finally:
                if heartbeat:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)

    async def test_busy_heartbeat_stops_at_original_lease_deadline(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        job['lease_until'] = await self.pool.fetchval("UPDATE v2_analysis_jobs SET lease_until=clock_timestamp()+interval '80 millisecond' WHERE id=$1 RETURNING lease_until", job['id'])
        with patch.object(worker, 'HEARTBEAT_SECONDS', .005):
            async with self.pool.acquire() as locker:
                async with locker.transaction():
                    await locker.execute('SELECT pg_advisory_xact_lock(hashtext($1))', worker.SCHEDULER_LOCK)
                    with self.assertRaises(worker.LeaseLost):
                        await asyncio.wait_for(worker.keep_lease(self.pool, job), 1)
        self.assertLessEqual(await self.pool.fetchval('SELECT lease_until FROM v2_analysis_jobs WHERE id=$1', job['id']), job['lease_until'])

    async def test_renewal_has_overall_deadline_and_cancels_delayed_attempt(self):
        cancelled = asyncio.Event()
        async def delayed(*args):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        with patch.object(worker, '_renew_lease_once', delayed):
            self.assertIsNone(await asyncio.wait_for(worker._renew_lease(self.pool, {}, budget=.02), 1))
        self.assertTrue(cancelled.is_set())

    async def test_bounded_locked_pair_probes_resume_from_persisted_turn(self):
        await self.make_queued()
        locked = [self.job]
        sources = [self.source]
        for _ in range(worker.MAX_PAIR_PROBES):
            source, expert = await self.add_expert()
            sources.append(source)
            locked.extend(await self.queued(source=source, expert=expert))
        ready_source, ready_expert = await self.add_expert()
        ready = (await self.queued(source=ready_source, expert=ready_expert))[0]
        for turn, source in enumerate([*sources, ready_source]):
            await self.pool.execute('INSERT INTO v2_review_source_schedule(source_id,last_turn) VALUES($1,$2)', source, turn)
        async with self.pool.acquire() as locker:
            async with locker.transaction():
                await locker.fetch('SELECT id FROM v2_analysis_jobs WHERE id=ANY($1::uuid[]) FOR UPDATE', locked)
                self.assertIsNone(await worker.claim_job(self.pool))
                claimed = await worker.claim_job(self.pool)
                self.assertEqual(ready, claimed['id'])
                payload = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='attempt_claimed' ORDER BY sequence_no DESC LIMIT 1", ready)
                self.assertEqual(2, payload['pair_probes'])

    async def test_locked_exhausted_job_does_not_block_other_expert(self):
        await self.pool.execute("UPDATE v2_analysis_jobs SET attempts=$2,lease_until=now()-interval '1 second' WHERE id=$1", self.job, worker.MAX_ATTEMPTS)
        source, expert = await self.add_expert()
        ready = (await self.queued(source=source, expert=expert))[0]
        async with self.pool.acquire() as locker:
            async with locker.transaction():
                await locker.fetchrow('SELECT id FROM v2_analysis_jobs WHERE id=$1 FOR UPDATE', self.job)
                claimed = await worker.claim_job(self.pool)
                self.assertEqual(ready, claimed['id'])
        self.assertIsNone(await worker.claim_job(self.pool))
        self.assertEqual('failed', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))

    async def test_locked_source_defers_claim_without_exiting_worker(self):
        await self.make_queued()
        async with self.pool.acquire() as locker:
            async with locker.transaction():
                await locker.execute('UPDATE v2_expert_sources SET enabled=enabled WHERE id=$1', self.source)
                with self.assertLogs(worker.log, level='WARNING') as captured:
                    self.assertIsNone(await asyncio.wait_for(worker.claim_job(self.pool), 4))
                self.assertIn('LockNotAvailableError', captured.output[-1])
        self.assertEqual('queued', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertIsNotNone(await worker.claim_job(self.pool))

    async def test_concurrent_claims_and_heartbeat_never_oversubscribe_or_replace_owner(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        await self.queued(count=5)
        second_pool = await self.second_pool()
        try:
            for _ in range(12):
                renewal, claimed = await asyncio.gather(worker._renew_lease(self.pool, job), worker.claim_job(second_pool))
                self.assertIsNone(claimed)
                self.assertIsNot(renewal, False)
            self.assertEqual(job['attempt_id'], await self.pool.fetchval('SELECT attempt_id FROM v2_analysis_jobs WHERE id=$1', job['id']))
        finally:
            await second_pool.close()

    async def test_source_stop_cancels_model_and_blocks_more_claims(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def model(*args):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        with patch.object(worker, 'HEARTBEAT_SECONDS', .01), patch.object(worker, 'review_message', model), patch.object(worker, 'delete_session', AsyncMock()):
            task = asyncio.create_task(worker.process_job(self.pool, SimpleNamespace(hermes_model='synthetic'), job))
            await asyncio.wait_for(started.wait(), 1)
            await self.pool.execute('UPDATE v2_expert_sources SET enabled=false WHERE id=$1', self.source)
            await asyncio.wait_for(task, 2)
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(await worker.claim_job(self.pool))
        self.assertEqual('retry', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', job['id']))

    async def test_worker_cancellation_keeps_reservation_until_expiry_then_recovers(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        started = asyncio.Event()
        async def model(*args):
            started.set()
            await asyncio.Future()
        with patch.object(worker, 'review_message', model), patch.object(worker, 'delete_session', AsyncMock()):
            task = asyncio.create_task(worker.process_job(self.pool, SimpleNamespace(hermes_model='synthetic'), job))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual('running', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', job['id']))
        self.assertIsNone(await worker.claim_job(self.pool))
        await self.pool.execute("UPDATE v2_analysis_jobs SET lease_until=now()-interval '1 second' WHERE id=$1", job['id'])
        recovered = await worker.claim_job(self.pool)
        self.assertNotEqual(job['attempt_id'], recovered['attempt_id'])

    async def test_schedule_keys_cannot_be_spoofed_and_copy_restore_preserves_references(self):
        from infra.recovery.bundle import insertion_order, schema
        await self.make_queued()
        await worker.claim_job(self.pool)
        source, expert = await self.add_expert()
        await self.pool.execute('UPDATE v2_analysis_jobs SET source_id=$2,expert_id=$3 WHERE id=$1', self.job, source, expert)
        row = await self.pool.fetchrow('SELECT source_id,expert_id FROM v2_analysis_jobs WHERE id=$1', self.job)
        self.assertEqual((self.source,self.expert), (row['source_id'],row['expert_id']))
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await self.pool.execute('UPDATE v2_expert_messages SET source_id=$2,expert_id=$3 WHERE id=$1', self.message, source, expert)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                names = [row['name'] for row in await schema(conn)]
                order = await insertion_order(conn, names)
                for parent in ('v2_expert_messages','v2_experts','v2_expert_sources'):
                    self.assertLess(order.index(parent), order.index('v2_analysis_jobs'))
                raw = io.BytesIO()
                async def sink(data): raw.write(data)
                await conn.copy_from_table('v2_analysis_jobs', output=sink, format='binary')
                await conn.execute('DELETE FROM v2_analysis_jobs')
                async def chunks(): yield raw.getvalue()
                await conn.copy_to_table('v2_analysis_jobs', source=chunks(), format='binary')
                restored = await conn.fetchrow('SELECT source_id,expert_id FROM v2_analysis_jobs WHERE id=$1', self.job)
                self.assertEqual((self.source,self.expert), (restored['source_id'],restored['expert_id']))

    async def test_migration_backfills_existing_jobs_and_future_inserts(self):
        name = 'test_review_legacy_' + uuid.uuid4().hex
        await self.admin.execute('CREATE DATABASE '+name)
        conn = await asyncpg.connect(self.admin_url.rsplit('/',1)[0]+'/'+name)
        try:
            await conn.execute('CREATE TABLE v2_schema_migrations(version TEXT PRIMARY KEY,checksum TEXT NOT NULL,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())')
            for version, checksum, sql in migration_files():
                if int(version.split('_')[0]) <= 11:
                    await conn.execute(sql)
                    await conn.execute('INSERT INTO v2_schema_migrations(version,checksum) VALUES($1,$2)', version, checksum)
            await conn.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'synthetic-old','Old')", self.source)
            await conn.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'synthetic-old','Old')", self.expert)
            await conn.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,'1','synthetic old',now())", self.message, self.source, self.expert)
            await conn.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key) VALUES($1,$2,'queued','synthetic-old')", self.job, self.message)
            applied = await apply_migrations(conn)
            self.assertIn('012_review_fair_scheduling.sql', applied)
            row = await conn.fetchrow('SELECT source_id,expert_id FROM v2_analysis_jobs WHERE id=$1', self.job)
            self.assertEqual((self.source,self.expert), (row['source_id'],row['expert_id']))
            self.assertEqual(1, await conn.fetchval('SELECT concurrency_limit FROM v2_review_expert_schedule WHERE expert_id=$1', self.expert))
        finally:
            await conn.close()
            await self.admin.execute('DROP DATABASE '+name+' WITH (FORCE)')


if __name__ == "__main__":
    unittest.main()
