"""Synthetic temporary-DB scheduling tests; no Telegram or model calls."""

import asyncio
from datetime import datetime, timedelta, timezone
import io
import json
import os
from unittest.mock import patch
import unittest
from urllib.parse import urlsplit
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import asyncpg

from app import worker
from app.database import apply_migrations, migration_files


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires task-owned temporary PostgreSQL')
class ReviewFreshnessSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.admin_url = os.environ['TEST_DATABASE_ADMIN_URL']
        parts = urlsplit(self.admin_url)
        if parts.hostname not in {'ai-football-v2-pgtest','ai-football-v2-pgverify'} or parts.path != '/postgres':
            raise RuntimeError('freshness scheduler tests require task-owned Docker PostgreSQL')
        self.name = 'test_review_freshness_' + uuid.uuid4().hex
        self.admin = await asyncpg.connect(self.admin_url)
        await self.admin.execute('CREATE DATABASE '+self.name)
        self.url = self.admin_url.rsplit('/',1)[0]+'/'+self.name
        self.pool = await self.new_pool()
        async with self.pool.acquire() as conn:
            await apply_migrations(conn)
        self.now = datetime.now(timezone.utc)

    async def asyncTearDown(self):
        await self.pool.close()
        await self.admin.execute('DROP DATABASE '+self.name+' WITH (FORCE)')
        await self.admin.close()

    async def new_pool(self):
        async def codecs(conn):
            await conn.set_type_codec('jsonb',schema='pg_catalog',encoder=json.dumps,decoder=json.loads)
        return await asyncpg.create_pool(self.url,min_size=1,max_size=4,init=codecs)

    async def pair(self,source=None,expert=None):
        source,expert = source or uuid.uuid4(),expert or uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,$2,'Synthetic source') ON CONFLICT DO NOTHING",source,str(source))
        await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,$2,'Synthetic expert') ON CONFLICT DO NOTHING",expert,str(expert))
        await self.pool.execute('INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2) ON CONFLICT DO NOTHING',source,expert)
        return source,expert

    async def queued(self,pair,*,age=0,count=1,state='queued',attempts=0):
        jobs = []
        for index in range(count):
            message,job = uuid.uuid4(),uuid.uuid4()
            posted = self.now-timedelta(seconds=age+index)
            await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,$4,'Synthetic recommendation',$5)",message,*pair,str(message),posted)
            await self.pool.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,attempts,available_at,lease_until,attempt_id) VALUES($1,$2,$3,$4,$5,now()-interval '2 days',CASE WHEN $3='running' THEN now()-interval '1 second' END,CASE WHEN $3='running' THEN $6::uuid END)",job,message,state,str(job),attempts,uuid.uuid4())
            jobs.append(job)
        return jobs

    async def complete(self,job):
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='unresolved',lease_until=NULL WHERE id=$1",job['id'])

    async def test_old_backlog_does_not_precede_current_messages_and_sources_stay_fair(self):
        pairs = [await self.pair() for _ in range(3)]
        for pair in pairs:
            await self.queued(pair,age=120000000,count=35)
            await self.queued(pair,age=60,count=4)
        original = [dict(row) for row in await self.pool.fetch('SELECT id,raw_text,posted_at FROM v2_expert_messages ORDER BY id')]
        seen = []
        with patch.object(worker,'HISTORICAL_CONCURRENCY',0):
            for _ in range(6):
                job = await worker.claim_job(self.pool)
                self.assertEqual('real_time',job['review_mode'])
                seen.append(job['source_id'])
                self.assertGreaterEqual(job['message']['posted_at'],self.now-timedelta(hours=24))
                await self.complete(job)
        for offset in (0,3):
            self.assertEqual({pair[0] for pair in pairs},set(seen[offset:offset+3]))
        self.assertEqual(original,[dict(row) for row in await self.pool.fetch('SELECT id,raw_text,posted_at FROM v2_expert_messages ORDER BY id')])
        self.assertEqual(105,await self.pool.fetchval("SELECT count(*) FROM v2_analysis_jobs WHERE state='queued' AND message_posted_at<now()-interval '24 hours'"))

    async def test_eight_to_one_budget_survives_new_pool_and_history_concurrency_is_global(self):
        realtime,historical = await self.pair(),await self.pair()
        await self.queued(realtime,age=60,count=12)
        await self.queued(historical,age=120000000,count=4)
        for _ in range(8):
            job = await worker.claim_job(self.pool)
            self.assertEqual('real_time',job['review_mode'])
            await self.complete(job)
        self.assertEqual(8,await self.pool.fetchval('SELECT realtime_claims_since_replay FROM v2_review_lane_schedule'))
        await self.pool.close()
        self.pool = await self.new_pool()
        replay = await worker.claim_job(self.pool)
        self.assertEqual('historical_replay',replay['review_mode'])
        self.assertEqual(0,await self.pool.fetchval('SELECT realtime_claims_since_replay FROM v2_review_lane_schedule'))
        # Even when another pool tries, only one replay can hold a valid lease.
        second = await self.new_pool()
        try:
            job = await worker.claim_job(second)
            self.assertEqual('real_time',job['review_mode'])
            self.assertEqual(1,await second.fetchval("SELECT count(*) FROM v2_analysis_jobs WHERE state='running' AND review_mode='historical_replay' AND lease_until>now()"))
        finally:
            await second.close()

    async def test_newest_posted_message_within_pair_wins_but_expired_attempt_recovers_first(self):
        pair = await self.pair()
        await self.queued(pair,age=3600,count=2)
        newest = (await self.queued(pair,age=1))[0]
        job = await worker.claim_job(self.pool)
        self.assertEqual(newest,job['id'])
        old_attempt = job['attempt_id']
        await self.pool.execute("UPDATE v2_analysis_jobs SET lease_until=now()-interval '1 second' WHERE id=$1",job['id'])
        await self.queued(pair,age=0)
        replacement = await worker.claim_job(self.pool)
        self.assertEqual(job['id'],replacement['id'])
        self.assertNotEqual(old_attempt,replacement['attempt_id'])
        self.assertEqual(2,replacement['attempts'])
        self.assertIs(False,await worker._renew_lease(self.pool,job))

    async def test_historical_expired_attempt_precedes_other_replay_and_keeps_original_time(self):
        pair = await self.pair()
        expired = (await self.queued(pair,age=120000000,state='running',attempts=2))[0]
        await self.queued(pair,age=90000)
        stamp = await self.pool.fetchval('SELECT message_posted_at FROM v2_analysis_jobs WHERE id=$1',expired)
        job = await worker.claim_job(self.pool)
        self.assertEqual(expired,job['id'])
        self.assertEqual(3,job['attempts'])
        self.assertEqual('historical_replay',job['review_mode'])
        self.assertEqual(stamp,job['message']['posted_at'])
        trace = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='attempt_claimed'",expired)
        self.assertEqual('historical_replay',trace['review_mode'])
        self.assertGreater(trace['message_age_seconds'],90000)

    async def test_authors_rotate_inside_each_lane_and_shared_expert_budget_remains_one(self):
        pair = await self.pair()
        other = await self.pair(source=pair[0])
        await self.queued(pair,age=60,count=12)
        await self.queued(other,age=60,count=12)
        with patch.object(worker,'HISTORICAL_CONCURRENCY',0):
            seen = []
            for _ in range(6):
                job = await worker.claim_job(self.pool)
                seen.append(job['expert_id'])
                await self.complete(job)
        for offset in (0,2,4):
            self.assertEqual({pair[1],other[1]},set(seen[offset:offset+2]))
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='unresolved',lease_until=NULL")
        cross = await self.pair(expert=pair[1])
        await self.queued(pair,age=60)
        await self.queued(cross,age=120000000)
        second = await self.new_pool()
        try:
            values = await asyncio.gather(worker.claim_job(self.pool),worker.claim_job(second))
            self.assertEqual(1,sum(value is not None for value in values))
        finally:
            await second.close()

    async def test_disabled_replay_future_messages_and_retry_delay_are_not_claimed(self):
        historical = await self.pair()
        await self.queued(historical,age=120000000)
        future = (await self.queued(await self.pair(),age=-3600))[0]
        delayed = (await self.queued(await self.pair(),age=30,state='retry'))[0]
        await self.pool.execute("UPDATE v2_analysis_jobs SET available_at=now()+interval '1 minute' WHERE id=$1",delayed)
        with patch.object(worker,'HISTORICAL_CONCURRENCY',0):
            self.assertIsNone(await worker.claim_job(self.pool))
        self.assertEqual('queued',await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1',future))
        replay = await worker.claim_job(self.pool)
        self.assertEqual('historical_replay',replay['review_mode'])

    async def test_projected_timestamp_cannot_be_spoofed_and_copy_restore_remains_valid(self):
        pair = await self.pair()
        job = (await self.queued(pair,age=120000000))[0]
        before = await self.pool.fetchrow('SELECT message_id,message_posted_at FROM v2_analysis_jobs WHERE id=$1',job)
        await self.pool.execute('UPDATE v2_analysis_jobs SET message_posted_at=now() WHERE id=$1',job)
        self.assertEqual(before['message_posted_at'],await self.pool.fetchval('SELECT message_posted_at FROM v2_analysis_jobs WHERE id=$1',job))
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await self.pool.execute('UPDATE v2_expert_messages SET posted_at=now() WHERE id=$1',before['message_id'])
        raw = io.BytesIO()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                async def sink(data): raw.write(data)
                await conn.copy_from_table('v2_analysis_jobs',output=sink,format='binary')
                await conn.execute('DELETE FROM v2_analysis_jobs')
                async def chunks(): yield raw.getvalue()
                await conn.copy_to_table('v2_analysis_jobs',source=chunks(),format='binary')
        self.assertEqual(before['message_posted_at'],await self.pool.fetchval('SELECT message_posted_at FROM v2_analysis_jobs WHERE id=$1',job))

    async def test_historical_sources_and_authors_rotate_without_erasing_backlog(self):
        first = await self.pair()
        second = await self.pair(source=first[0])
        third = await self.pair()
        for pair in (first,second,third):
            await self.queued(pair,age=120000000,count=5)
        seen = []
        for _ in range(9):
            job = await worker.claim_job(self.pool)
            self.assertEqual('historical_replay',job['review_mode'])
            seen.append((job['source_id'],job['expert_id']))
            await self.complete(job)
        for offset in range(0,8,2):
            self.assertEqual({first[0],third[0]},{value[0] for value in seen[offset:offset+2]})
        authors = [expert for source,expert in seen if source == first[0]]
        for offset in range(0,len(authors)-1,2):
            self.assertEqual({first[1],second[1]},set(authors[offset:offset+2]))
        self.assertEqual(15,await self.pool.fetchval('SELECT count(*) FROM v2_expert_messages'))
        self.assertEqual(6,await self.pool.fetchval("SELECT count(*) FROM v2_analysis_jobs WHERE state='queued'"))

    async def test_exact_freshness_boundary_is_realtime_and_future_time_is_neither_lane(self):
        realtime = worker._lane_condition('real_time')
        historical = worker._lane_condition('historical_replay')
        rows = await self.pool.fetch(
            "SELECT $1::int AS attempts,label,("+realtime+") AS realtime,("+historical+") AS historical "
            "FROM (VALUES ('boundary',statement_timestamp()-($2::int*interval '1 second')),"
            "('old',statement_timestamp()-($2::int*interval '1 second')-interval '1 microsecond'),"
            "('now',statement_timestamp()),('future',statement_timestamp()+interval '1 microsecond')) j(label,message_posted_at)",
            worker.MAX_ATTEMPTS,worker.REALTIME_WINDOW_SECONDS)
        result = {row['label']:(row['realtime'],row['historical']) for row in rows}
        self.assertEqual({'boundary':(True,False),'old':(False,True),'now':(True,False),'future':(False,False)},result)

    async def test_migration_preserves_preexisting_attempts_and_backfills_true_posting_time(self):
        name = 'test_review_freshness_legacy_'+uuid.uuid4().hex
        await self.admin.execute('CREATE DATABASE '+name)
        conn = await asyncpg.connect(self.admin_url.rsplit('/',1)[0]+'/'+name)
        try:
            await conn.execute('CREATE TABLE v2_schema_migrations(version TEXT PRIMARY KEY,checksum TEXT NOT NULL,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())')
            for version,checksum,sql in migration_files():
                if int(version.split('_')[0]) <= 16:
                    await conn.execute(sql)
                    await conn.execute('INSERT INTO v2_schema_migrations(version,checksum) VALUES($1,$2)',version,checksum)
            source,expert,message,job,attempt = [uuid.uuid4() for _ in range(5)]
            posted = self.now-timedelta(days=1380)
            await conn.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'synthetic-legacy','Legacy')",source)
            await conn.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'synthetic-legacy','Legacy')",expert)
            await conn.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,'1','Original historical text',$4)",message,source,expert,posted)
            await conn.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,attempts,attempt_id,lease_until) VALUES($1,$2,'running','legacy-attempt',2,$3,now()+interval '1 minute')",job,message,attempt)
            old_job = dict(await conn.fetchrow('SELECT * FROM v2_analysis_jobs WHERE id=$1',job))
            old_message = dict(await conn.fetchrow('SELECT * FROM v2_expert_messages WHERE id=$1',message))
            applied = await apply_migrations(conn)
            expected = [version for version, _, _ in migration_files() if int(version.split('_')[0]) > 16]
            self.assertEqual(expected,applied)
            self.assertEqual('017_review_freshness_lanes.sql',applied[0])
            migrated = dict(await conn.fetchrow('SELECT * FROM v2_analysis_jobs WHERE id=$1',job))
            self.assertEqual(old_job,{key:migrated[key] for key in old_job})
            self.assertEqual(posted,migrated['message_posted_at'])
            self.assertEqual('historical_replay',migrated['review_mode'])
            self.assertEqual(old_message,dict(await conn.fetchrow('SELECT * FROM v2_expert_messages WHERE id=$1',message)))
            self.assertEqual([],await apply_migrations(conn))
        finally:
            await conn.close()
            await self.admin.execute('DROP DATABASE '+name+' WITH (FORCE)')

    async def test_historical_mode_reaches_model_as_copy_without_changing_original_message(self):
        pair = await self.pair()
        await self.queued(pair,age=120000000)
        job = await worker.claim_job(self.pool)
        before = dict(job['message'])
        model = AsyncMock(return_value=('Historical archive only',{'content':'Historical archive only'}))
        settings = SimpleNamespace(hermes_model='isolated-model')
        with patch.object(worker,'review_message',model),patch.object(worker,'delete_session',AsyncMock()), \
                patch.object(worker,'resolve_review_settings',AsyncMock(return_value=settings)):
            await worker.process_job(self.pool,settings,job)
        self.assertEqual('historical_replay',model.call_args.args[1]['review_mode'])
        self.assertEqual(before,job['message'])
        self.assertEqual('unresolved',await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1',job['id']))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
