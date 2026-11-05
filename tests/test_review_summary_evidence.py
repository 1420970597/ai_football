"""Real database contracts for receipt evidence and its remote read budget."""
from datetime import datetime, timezone
import os
import unittest
import uuid

from app.review_state import trace
from app.review_summary import make_summary, record_summary
from app.review_summary_evidence import read_summary_evidence
import test_reviews as support


class CountedConnection:
    def __init__(self, conn):
        self.conn = conn
        self.reads = []

    async def fetchrow(self, query, *args):
        self.reads.append(query)
        return await self.conn.fetchrow(query, *args)

    def __getattr__(self, name):
        return getattr(self.conn, name)


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class SummaryEvidenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    fixture = support.ReviewTests.fixture

    async def evidence(self):
        async with self.pool.acquire() as conn:
            counted = CountedConnection(conn)
            result = await read_summary_evidence(counted, self.job, self.attempt, self.message, 100)
            self.assertEqual(1, len(counted.reads))
            return result

    async def test_empty_snapshot_is_complete_and_uses_one_read(self):
        result = await self.evidence()
        self.assertEqual((0, [], [], [], []), (
            result.total, result.signals, result.rejected, result.identified, result.model_contents))

    async def test_attempt_isolation_and_trace_order_preserve_unverified_mentions(self):
        async with self.pool.acquire() as conn, conn.transaction():
            for attempt, kind, payload in (
                (uuid.uuid4(), 'mcp_record_expert_signals', {'rejected': [{'reason': 'old'}]}),
                (uuid.uuid4(), 'hermes_result', {'content': 'leyu:999'}),
                (self.attempt, 'hermes_partial_result', {'content': 'leyu:123'}),
                (self.attempt, 'mcp_record_expert_signals', {'rejected': [{'reason': 'fixture_not_active'}]}),
                (self.attempt, 'mcp_record_expert_signals', {}),
                (self.attempt, 'hermes_result', {'content': 'leyu:456'}),
            ):
                await trace(conn, self.job, kind, {'attempt_id': str(attempt), **payload})
        result = await self.evidence()
        self.assertEqual([{'reason': 'fixture_not_active'}], result.rejected)
        self.assertEqual(['leyu:123', 'leyu:456'], result.model_contents)
        async with self.pool.acquire() as conn, conn.transaction():
            counted = CountedConnection(conn)
            await record_summary(counted, {'id': self.job, 'message_id': self.message}, self.attempt, 'real_time')
            self.assertEqual(2, len(counted.reads), 'snapshot plus independent pagination verification')
        receipt = await self.pool.fetchval(
            "SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='system_review_result'", self.job)
        self.assertFalse(receipt['completion_verified'])
        self.assertEqual(2, receipt['completion_issues']['unbound_mention_count'])

    async def test_counts_limits_attempt_bindings_and_decimal_time_contract(self):
        stamp = datetime(2026, 1, 2, 3, 4, 5, 123000, timezone.utc)
        await self.pool.execute(
            "INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase,source_updated_at) "
            "SELECT 'bulk-'||i,'bulk-'||i,'football','League','Home'||i,'Away'||i,$1,'scheduled',$1 "
            "FROM generate_series(1,207) i", stamp)
        await self.pool.execute(
            "INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,"
            "market_kind,line,selection,recommendation_price,quote_time,confidence,created_at) "
            "SELECT md5(i::text)::uuid,$1,$2,'bulk-'||i,'scheduled','total','Total','total',"
            "2.750,'over',1.2345,$3,1,$3 FROM generate_series(1,101) i", self.message, self.expert, stamp)
        await self.pool.execute(
            "INSERT INTO v2_review_fixture_bindings(job_id,attempt_id,fixture_id,search_key,evidence) "
            "SELECT $1,$2,'bulk-'||i,'test','{}'::jsonb FROM generate_series(1,206) i", self.job, self.attempt)
        await self.pool.execute(
            "INSERT INTO v2_review_fixture_bindings(job_id,attempt_id,fixture_id,search_key,evidence) "
            "VALUES($1,$2,'bulk-207','old','{}')", self.job, uuid.uuid4())
        result = await self.evidence()
        self.assertEqual(101, result.total)
        self.assertEqual(100, len(result.signals))
        self.assertEqual(100, len(result.identified))
        self.assertEqual(105, result.identified[0]['total'])
        self.assertEqual(sorted(row['signal_id'] for row in result.signals), [row['signal_id'] for row in result.signals])
        self.assertFalse({row['fixture_id'] for row in result.signals} & {row['fixture_id'] for row in result.identified})
        receipt = make_summary(result.signals, accepted_total=result.total, rejected=[], review_mode='real_time',
                               identified=result.identified, identified_total=result.identified[0]['total'])
        self.assertEqual((1, 5), (receipt['omitted_count'], receipt['identified_omitted_count']))
        self.assertEqual('2.750', receipt['items'][0]['line'])
        native_price = await self.pool.fetchval('SELECT recommendation_price FROM v2_expert_signals LIMIT 1')
        self.assertEqual(str(native_price), receipt['items'][0]['recommendation_price'])
        self.assertEqual(stamp.isoformat(), receipt['items'][0]['quote_time'])
        self.assertEqual(stamp.isoformat(), receipt['items'][0]['kickoff_at'])
        self.assertNotIn('bulk-207', {row['fixture_id'] for row in result.identified})

    async def test_receipt_write_rolls_back_with_completion_transaction(self):
        class AbortCompletion(Exception):
            pass
        async with self.pool.acquire() as conn:
            with self.assertRaises(AbortCompletion):
                async with conn.transaction():
                    await record_summary(conn, {'id': self.job, 'message_id': self.message}, self.attempt, 'real_time')
                    self.assertEqual(1, await conn.fetchval(
                        "SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='system_review_result'"))
                    raise AbortCompletion()
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_traces'))
        self.assertEqual('running', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
