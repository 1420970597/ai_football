"""Real database contracts for receipt evidence and its remote read budget."""
from datetime import datetime, timezone
import os
import unittest
import uuid

from app.review_state import trace
from app.review_summary import make_summary, record_summary
from app.review_summary_evidence import read_summary_evidence
from app.review_completion import completion_issues
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
        self.assertEqual({}, result.pagination_issues)

    async def test_no_mentions_receipt_uses_snapshot_pagination_and_one_read(self):
        await self.pool.execute(
            "INSERT INTO v2_match_searches(job_id,attempt_id,search_key,team_query,phase,expected_cursor) "
            "VALUES($1,$2,'unfinished','Alpha','all','next-page')", self.job, self.attempt)
        scope = {'sport': 'basketball', 'phase': 'all', 'from_utc': '2026-11-06T00:00:00Z'}
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'mcp_search_matches', {
                'attempt_id': str(self.attempt), 'search_key': 'unfinished',
                'status': 'succeeded', 'scope': scope})
            await trace(conn, self.job, 'hermes_result', {
                'attempt_id': str(self.attempt), 'content': '聊天，未进行推荐'})
            expected = await completion_issues(conn, self.job, self.attempt, set())
            counted = CountedConnection(conn)
            await record_summary(counted, {'id': self.job, 'message_id': self.message}, self.attempt, 'real_time')
            self.assertEqual(1, len(counted.reads))
        receipt = await self.pool.fetchval(
            "SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertEqual(expected, receipt['completion_issues'])
        self.assertFalse(receipt['completion_verified'])
        self.assertEqual('next-page', expected['unfinished_searches'][0]['cursor'])
        self.assertEqual(scope, expected['unfinished_searches'][0]['scope'])

    async def test_snapshot_pagination_preserves_real_count_limit_and_attempt_scope(self):
        await self.pool.execute(
            "INSERT INTO v2_match_searches(job_id,attempt_id,search_key,team_query,phase,expected_cursor) "
            "SELECT $1,$2,'current-'||i,'Alpha','all','cursor-'||i FROM generate_series(1,105)i",
            self.job, self.attempt)
        await self.pool.execute(
            "INSERT INTO v2_match_searches(job_id,attempt_id,search_key,team_query,phase) "
            "VALUES($1,$2,'old-attempt','Alpha','all'),($1,$3,'completed','Alpha','all')",
            self.job, uuid.uuid4(), self.attempt)
        await self.pool.execute(
            "UPDATE v2_match_searches SET complete=true WHERE search_key='completed'")
        result = await self.evidence()
        async with self.pool.acquire() as conn:
            expected = await completion_issues(conn, self.job, self.attempt, set())
        self.assertEqual(expected, result.pagination_issues)
        self.assertEqual(105, result.pagination_issues['unfinished_search_count'])
        self.assertEqual(100, len(result.pagination_issues['unfinished_searches']))
        self.assertNotIn('old-attempt', {r['search_key'] for r in result.pagination_issues['unfinished_searches']})

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


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class RecoveredRejectionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    fixture = support.ReviewTests.fixture
    evidence = SummaryEvidenceTests.evidence
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    search_all = support.ReviewTests.search_all
    resolve = support.ReviewTests.resolve
    read_market = support.ReviewTests.read_market

    async def accepted_pick(self):
        fixture = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        quote = await self.pool.fetchval('SELECT id FROM v2_current_quotes WHERE fixture_id=$1', fixture)
        pick = {'fixture_id': fixture, 'quote_id': str(quote), 'evidence_text': '全场大2.5'}
        rejected = await self.post('record_expert_signals', signals=[pick])
        self.assertEqual('quote_not_read_or_changed', rejected.json()['rejected'][0]['reason'])
        await self.read_market(fixture)
        accepted = await self.post('record_expert_signals', signals=[pick])
        self.assertEqual(1, len(accepted.json()['accepted']))
        return pick, accepted.json()['accepted'][0]['signal_id']

    async def test_later_system_acceptance_removes_only_final_unresolved_and_keeps_failure_trace(self):
        await self.accepted_pick()
        result = await self.evidence()
        self.assertEqual(1, result.total)
        self.assertEqual([], result.rejected)
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='mcp_record_expert_signals' AND jsonb_array_length(payload->'rejected')>0"))

    async def test_other_quote_fixture_and_missing_or_invalid_index_remain_unresolved(self):
        pick, _ = await self.accepted_pick()
        cases = [({'fixture_id': pick['fixture_id'], 'quote_id': str(uuid.uuid4())}, 0),
                 ({'fixture_id': 'another-fixture', 'quote_id': pick['quote_id']}, 0),
                 (pick, 'bad'), (pick, -1), (pick, 100000000000000000000), (pick, None)]
        async with self.pool.acquire() as conn, conn.transaction():
            for wrong, index in cases:
                await trace(conn, self.job, 'mcp_record_expert_signals', {
                    'attempt_id': str(self.attempt), 'parameters': {'signals': [wrong]},
                    'rejected': [{'index': index, 'reason': 'quote_not_read_or_changed'}]})
        result = await self.evidence()
        self.assertEqual(len(cases), len(result.rejected))

    async def test_signal_without_immutable_quote_evidence_does_not_clear_rejection(self):
        _, signal_id = await self.accepted_pick()
        await self.pool.execute('DELETE FROM v2_quote_evidence WHERE reference_id=$1', uuid.UUID(signal_id))
        self.assertEqual(1, len((await self.evidence()).rejected))

    async def test_another_message_signal_evidence_cannot_clear_this_message_rejection(self):
        _, signal_id = await self.accepted_pick()
        other = uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,'different','other',now())", other, self.source, self.expert)
        await self.pool.execute('UPDATE v2_expert_signals SET message_id=$2 WHERE id=$1', uuid.UUID(signal_id), other)
        self.assertEqual(1, len((await self.evidence()).rejected))
