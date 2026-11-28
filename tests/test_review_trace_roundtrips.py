"""Remote round-trip reduction must preserve lease guards and trace ordering."""
import asyncio
import os
import unittest
import uuid

from fastapi import HTTPException
from app import worker
import test_reviews as support


class CountedConnection:
    def __init__(self, conn, calls):
        self.conn, self.calls = conn, calls

    async def fetchrow(self, query, *args):
        self.calls.append('fetchrow')
        return await self.conn.fetchrow(query, *args)

    async def execute(self, query, *args):
        self.calls.append('execute')
        return await self.conn.execute(query, *args)

    def __getattr__(self, name):
        return getattr(self.conn, name)


class CountedPool:
    def __init__(self, pool):
        self.pool, self.calls = pool, []

    def acquire(self):
        holder = self.pool.acquire()
        calls = self.calls

        class Acquisition:
            async def __aenter__(self):
                return CountedConnection(await holder.__aenter__(), calls)

            async def __aexit__(self, *args):
                return await holder.__aexit__(*args)

        return Acquisition()


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class TraceRoundTripTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown

    def job_body(self, **updates):
        return {'id': self.job, 'attempt_id': self.attempt, **updates}

    async def test_write_uses_two_statements_and_commits_raw_result(self):
        counted = CountedPool(self.pool)
        await worker._add_trace(counted, self.job_body(), 'hermes_result', {'content': '原始模型结果'})
        self.assertEqual(['fetchrow', 'execute'], counted.calls)
        row = await self.pool.fetchrow('SELECT sequence_no,trace_kind,payload FROM v2_analysis_traces WHERE job_id=$1', self.job)
        self.assertEqual(1, row['sequence_no'])
        self.assertEqual('hermes_result', row['trace_kind'])
        self.assertEqual({'attempt_id': str(self.attempt), 'content': '原始模型结果'}, row['payload'])

    async def test_concurrent_writers_wait_then_allocate_unique_continuous_sequences(self):
        await asyncio.gather(*(worker._add_trace(self.pool, self.job_body(), 'hermes_result', {'index': i}) for i in range(12)))
        rows = await self.pool.fetch('SELECT sequence_no,payload FROM v2_analysis_traces WHERE job_id=$1 ORDER BY sequence_no', self.job)
        self.assertEqual(list(range(1, 13)), [r['sequence_no'] for r in rows])
        self.assertEqual(set(range(12)), {r['payload']['index'] for r in rows})

    async def test_stale_attempt_and_disabled_source_do_not_write(self):
        for job in (self.job_body(attempt_id=uuid.uuid4()), self.job_body()):
            if job['attempt_id'] == self.attempt:
                await self.pool.execute('UPDATE v2_expert_sources SET enabled=false WHERE id=$1', self.source)
            with self.assertRaises(HTTPException) as raised:
                await worker._add_trace(self.pool, job, 'hermes_result', {'content': 'must not persist'})
            self.assertEqual(409, raised.exception.status_code)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_traces'))

    async def test_failed_insert_releases_validation_and_sequence_locks_for_retry(self):
        await self.pool.execute("CREATE FUNCTION reject_trace() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'isolated trace failure'; END $$")
        await self.pool.execute('CREATE TRIGGER reject_trace BEFORE INSERT ON v2_analysis_traces FOR EACH ROW EXECUTE FUNCTION reject_trace()')
        with self.assertRaisesRegex(Exception, 'isolated trace failure'):
            await worker._add_trace(self.pool, self.job_body(), 'hermes_result', {'content': 'rolled back'})
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_traces'))
        await self.pool.execute('DROP TRIGGER reject_trace ON v2_analysis_traces')
        await worker._add_trace(self.pool, self.job_body(), 'hermes_result', {'content': 'retry'})
        self.assertEqual(1, await self.pool.fetchval('SELECT sequence_no FROM v2_analysis_traces'))
