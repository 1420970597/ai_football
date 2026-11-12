"""The detection budget is shared by every stage and fenced before tool commit.

Only protocol fixtures and task-owned PostgreSQL databases are used. A missed
deadline is an unresolved outcome, never evidence of successful identification.
"""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from fastapi import HTTPException

from app import ingestion, review_deadline, review_state, review_tool_evidence, worker
from app.routes import reviews
import test_reviews as support


DETECTED = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class ReviewDeadlineBudgetTests(unittest.TestCase):
    def test_budget_is_twenty_seconds_from_detection_not_start_of_each_stage(self):
        self.assertEqual(20, review_deadline.REALTIME_SIGNAL_DEADLINE_SECONDS)
        self.assertEqual(DETECTED + timedelta(seconds=20),
                         review_deadline.review_deadline_at(DETECTED))
        for elapsed, remaining in ((0, 20), (6.125, 13.875), (19.999, .001),
                                   (20, 0), (90, 0)):
            with self.subTest(elapsed=elapsed):
                self.assertAlmostEqual(remaining, review_deadline.remaining_review_seconds(
                    DETECTED, now=DETECTED + timedelta(seconds=elapsed)), places=6)

    def test_unknown_legacy_detection_does_not_invent_a_measurement(self):
        self.assertIsNone(review_deadline.review_deadline_at(None))
        self.assertIsNone(review_deadline.remaining_review_seconds(None, now=DETECTED))

    def test_equivalent_timezone_preserves_one_absolute_deadline(self):
        local = DETECTED.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(review_deadline.review_deadline_at(DETECTED),
                         review_deadline.review_deadline_at(local))
        self.assertEqual(3.5, review_deadline.remaining_review_seconds(
            local, now=DETECTED + timedelta(seconds=16.5)))


class ReviewWorkerDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def job(self, *, detected_at=DETECTED, mode='real_time'):
        return {'id': uuid.uuid4(), 'attempt_id': uuid.uuid4(), 'attempts': 1,
                'review_mode': mode, 'message': {'detected_at': detected_at}}

    @contextmanager
    def clock(self, instant):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.replace(tzinfo=None) if tz is None else instant.astimezone(tz)

        with patch.object(review_deadline, 'datetime', Clock):
            yield

    async def test_already_expired_detection_never_starts_model_or_preparation(self):
        job, pool, settings = self.job(), object(), object()
        process, expire = AsyncMock(), AsyncMock()
        with self.clock(DETECTED + timedelta(seconds=20)), \
             patch.object(worker, '_process_job', process), \
             patch.object(worker, '_expire_detection_deadline', expire):
            await worker.process_job(pool, settings, job)
        process.assert_not_awaited()
        expire.assert_awaited_once_with(pool, job)

    async def test_queue_age_is_subtracted_once_from_the_complete_processing_budget(self):
        job, pool, settings, client = self.job(), object(), object(), object()
        process = AsyncMock()
        deadlines = []
        original_timeout = asyncio.timeout

        def timeout(seconds):
            deadlines.append(seconds)
            return original_timeout(seconds)

        with self.clock(DETECTED + timedelta(seconds=6.25)), \
             patch.object(worker.asyncio, 'timeout', side_effect=timeout), \
             patch.object(worker, '_process_job', process):
            await worker.process_job(pool, settings, job, model_client=client)
        self.assertEqual([13.75], deadlines)
        process.assert_awaited_once_with(pool, settings, job, model_client=client)

    async def test_budget_cancels_and_drains_the_entire_processing_stage_then_marks_unresolved(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        job, pool, settings = self.job(), object(), object()
        expire = AsyncMock()

        async def pending(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with patch.object(worker, 'remaining_review_seconds', return_value=.02), \
             patch.object(worker, '_process_job', side_effect=pending), \
             patch.object(worker, '_expire_detection_deadline', expire):
            await asyncio.wait_for(worker.process_job(pool, settings, job), .5)
        self.assertTrue(started.is_set())
        self.assertTrue(stopped.is_set())
        expire.assert_awaited_once_with(pool, job)

    async def test_slow_expert_does_not_consume_another_experts_remaining_budget(self):
        slow, fresh = self.job(), self.job(detected_at=DETECTED + timedelta(seconds=15))
        slow_started, slow_stopped = asyncio.Event(), asyncio.Event()
        fresh_completed = asyncio.Event()
        expire = AsyncMock()

        async def process(pool, settings, job, **kwargs):
            if job is fresh:
                await slow_started.wait()
                fresh_completed.set()
                return
            slow_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                slow_stopped.set()

        def budget(detected_at):
            return .02 if detected_at == slow['message']['detected_at'] else .2

        with patch.object(worker, 'remaining_review_seconds', side_effect=budget), \
             patch.object(worker, '_process_job', side_effect=process), \
             patch.object(worker, '_expire_detection_deadline', expire):
            await asyncio.wait_for(asyncio.gather(
                worker.process_job(object(), object(), slow),
                worker.process_job(object(), object(), fresh)), .5)
        self.assertTrue(fresh_completed.is_set())
        self.assertTrue(slow_stopped.is_set())
        self.assertEqual(1, expire.await_count)
        self.assertIs(slow, expire.await_args.args[1])

    async def test_legacy_unknown_and_historical_review_do_not_claim_a_known_twenty_second_budget(self):
        for job in (self.job(detected_at=None), self.job(mode='historical_replay')):
            process, expire = AsyncMock(), AsyncMock()
            with self.subTest(mode=job['review_mode'], detected_at=job['message']['detected_at']), \
                 patch.object(worker, '_process_job', process), \
                 patch.object(worker, '_expire_detection_deadline', expire):
                await worker.process_job(object(), object(), job)
            process.assert_awaited_once()
            expire.assert_not_awaited()

    async def test_shutdown_cancellation_is_not_misclassified_as_a_detection_timeout(self):
        started = asyncio.Event()
        expire = AsyncMock()

        async def pending(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        with patch.object(worker, 'remaining_review_seconds', return_value=20), \
             patch.object(worker, '_process_job', side_effect=pending), \
             patch.object(worker, '_expire_detection_deadline', expire):
            task = asyncio.create_task(worker.process_job(object(), object(), self.job()))
            await asyncio.wait_for(started.wait(), .5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, .5)
        expire.assert_not_awaited()


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class ReviewDetectionFenceTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture
    search_all = support.ReviewTests.search_all
    resolve = support.ReviewTests.resolve
    read_market = support.ReviewTests.read_market

    async def asyncSetUp(self):
        await support.ReviewTests.asyncSetUp(self)
        # Fixed UTC instants make the boundary independent of machine load.
        self.now = DETECTED
        self.clock_now = DETECTED
        # The shared fixture already inserted a job whose copied posted_at is
        # protected by a composite FK. Rebuild that empty isolated job after
        # fixing the message time, letting the normal insert trigger copy it.
        await self.pool.execute('DELETE FROM v2_analysis_jobs WHERE id=$1', self.job)
        await self.pool.execute(
            'UPDATE v2_expert_messages SET detected_at=$2,posted_at=$2 WHERE id=$1',
            self.message, DETECTED)
        await self.pool.execute(
            'INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,attempt_id,lease_until) '
            "VALUES($1,$2,'running',$3,$4,$5)", self.job, self.message, str(self.job),
            self.attempt, DETECTED + timedelta(minutes=5))

    @contextmanager
    def clock(self):
        owner = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                value = owner.clock_now
                return value.replace(tzinfo=None) if tz is None else value.astimezone(tz)

        with patch.object(review_state, 'datetime', Clock), \
             patch.object(review_deadline, 'datetime', Clock), \
             patch.object(reviews, 'datetime', Clock), \
             patch.object(ingestion, 'datetime', Clock):
            yield

    def assert_deadline_rejection(self, error):
        self.assertEqual(409, error.status_code)
        self.assertIn('detection_deadline_exceeded', json.dumps(error.detail))

    async def active(self, *, enforce_deadline=True, attempt=None):
        async with self.pool.acquire() as conn, conn.transaction():
            return await review_state.active_job(
                conn, self.job, attempt or self.attempt,
                enforce_deadline=enforce_deadline)

    async def test_valid_lease_does_not_authorize_a_late_recommendation(self):
        with self.clock():
            self.clock_now = DETECTED + timedelta(seconds=19.999)
            self.assertEqual(self.message, (await self.active())['message_id'])
            self.clock_now = DETECTED + timedelta(seconds=20)
            with self.assertRaises(HTTPException) as rejected:
                await self.active()
            self.assert_deadline_rejection(rejected.exception)
        self.assertGreater(await self.pool.fetchval(
            'SELECT lease_until FROM v2_analysis_jobs WHERE id=$1', self.job), self.clock_now)

    async def test_expired_detection_can_be_read_for_audit_without_authorizing_tools(self):
        self.clock_now = DETECTED + timedelta(seconds=60)
        with self.clock():
            result = await self.active(enforce_deadline=False)
            self.assertEqual(self.message, result['message_id'])
            with self.assertRaises(HTTPException) as rejected:
                await self.active()
            self.assert_deadline_rejection(rejected.exception)

    async def test_unknown_legacy_detection_stays_distinct_from_a_verified_fast_result(self):
        await self.pool.execute('UPDATE v2_expert_messages SET detected_at=NULL WHERE id=$1', self.message)
        self.clock_now = DETECTED + timedelta(seconds=60)
        with self.clock():
            self.assertIsNone((await self.active())['detected_at'])

    async def test_new_attempt_or_renewed_lease_does_not_restart_detection_budget(self):
        replacement = uuid.uuid4()
        await self.pool.execute(
            'UPDATE v2_analysis_jobs SET attempt_id=$2,lease_until=$3 WHERE id=$1',
            self.job, replacement, DETECTED + timedelta(hours=1))
        self.clock_now = DETECTED + timedelta(seconds=21)
        with self.clock():
            with self.assertRaises(HTTPException) as rejected:
                await self.active(attempt=replacement)
            self.assert_deadline_rejection(rejected.exception)
        self.assertEqual(DETECTED, await self.pool.fetchval(
            'SELECT detected_at FROM v2_expert_messages WHERE id=$1', self.message))

    async def test_every_tool_rejects_after_deadline_before_creating_search_or_signal(self):
        self.clock_now = DETECTED + timedelta(seconds=20)
        calls = [
            ('search_matches', {'team_query': 'Alpha', 'phase': 'all'}),
            ('resolve_match', {'search_key': 'a' * 64, 'team_a': 'Alpha1', 'team_b': 'Beta1'}),
            ('list_match_markets', {'fixture_id': 'fixture-1'}),
            ('record_expert_signals', {'signals': [{
                'fixture_id': 'fixture-1', 'quote_id': str(uuid.uuid4()),
                'evidence_text': 'Synthetic explicit full-time over 2.5'}]}),
        ]
        with self.clock():
            for tool, payload in calls:
                with self.subTest(tool=tool):
                    response = await self.post(tool, **payload)
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertIn('detection_deadline_exceeded', json.dumps(response.json()))
        for table in ('v2_match_searches', 'v2_review_fixture_bindings',
                      'v2_review_market_reads', 'v2_expert_signals', 'v2_quote_evidence'):
            self.assertEqual(0, await self.pool.fetchval(f'SELECT count(*) FROM {table}'))

    async def test_deadline_crossed_after_signal_insert_rolls_back_signal_and_price_evidence(self):
        with self.clock():
            fixture = await self.fixture()
            key, _ = await self.search_all()
            await self.resolve(key)
            quote = await self.read_market(fixture)
            original = reviews.persist_signals
            inserted = []

            async def cross_deadline(conn, **kwargs):
                result = await original(conn, **kwargs)
                inserted.append(await conn.fetchval('SELECT count(*) FROM v2_expert_signals'))
                # A real tool can finish its work after cancellation. The API
                # finalizer must apply the same persisted deadline independently.
                self.clock_now = DETECTED + timedelta(seconds=20)
                return result

            self.clock_now = DETECTED + timedelta(seconds=19)
            with patch.object(reviews, 'persist_signals', side_effect=cross_deadline):
                response = await self.post('record_expert_signals', signals=[{
                    'fixture_id': fixture, 'quote_id': quote['quote_id'],
                    'evidence_text': 'Alpha1 Beta1 full-time over 2.5'}])
            self.assertEqual([1], inserted, 'the test must cross the deadline after a real insert')
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn('detection_deadline_exceeded', json.dumps(response.json()))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))
        self.assertEqual(0, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='mcp_record_expert_signals' "
            "AND payload->>'status'='succeeded'"))

    async def test_deadline_crossed_after_success_trace_rolls_back_trace_signal_and_evidence(self):
        with self.clock():
            fixture = await self.fixture()
            key, _ = await self.search_all()
            await self.resolve(key)
            quote = await self.read_market(fixture)
            original = review_tool_evidence.append_traces
            uncommitted_counts = []

            async def cross_deadline(conn, review_id, records, **kwargs):
                await original(conn, review_id, records, **kwargs)
                if any(kind == 'mcp_record_expert_signals' for kind, _ in records):
                    counts = await conn.fetchrow(
                        'SELECT (SELECT count(*) FROM v2_expert_signals) AS signals,'
                        '(SELECT count(*) FROM v2_quote_evidence) AS evidence,'
                        "(SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='mcp_record_expert_signals' "
                        "AND payload->>'status'='succeeded') AS successes")
                    uncommitted_counts.append(tuple(counts.values()))
                    self.clock_now = DETECTED + timedelta(seconds=20)

            self.clock_now = DETECTED + timedelta(seconds=19)
            with patch.object(review_tool_evidence, 'append_traces', side_effect=cross_deadline):
                response = await self.post('record_expert_signals', signals=[{
                    'fixture_id': fixture, 'quote_id': quote['quote_id'],
                    'evidence_text': 'Alpha1 Beta1 full-time over 2.5'}])
            self.assertEqual([(1, 1, 1)], uncommitted_counts,
                             'signal, price evidence and success trace must really exist before crossing')
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn('detection_deadline_exceeded', json.dumps(response.json()))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))
        self.assertEqual(0, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='mcp_record_expert_signals' "
            "AND payload->>'status'='succeeded'"))

    async def test_one_expired_expert_does_not_reject_another_experts_concurrent_tool(self):
        expert, message, job, attempt = [uuid.uuid4() for _ in range(4)]
        await self.pool.execute(
            "INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,$2,'Synthetic second expert')",
            expert, str(expert))
        await self.pool.execute('INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)', self.source, expert)
        await self.pool.execute(
            'INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at,detected_at) '
            "VALUES($1,$2,$3,$4,'Synthetic second expert explicit intent',$5,$5)",
            message, self.source, expert, str(message), DETECTED + timedelta(seconds=15))
        await self.pool.execute(
            'INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,attempt_id,lease_until) '
            "VALUES($1,$2,'running',$3,$4,$5)", job, message, str(job), attempt,
            DETECTED + timedelta(minutes=5))
        self.clock_now = DETECTED + timedelta(seconds=21)

        async def fresh_expert():
            async with self.pool.acquire() as conn, conn.transaction():
                return await review_state.active_job(conn, job, attempt, enforce_deadline=True)

        with self.clock():
            expired, fresh = await asyncio.gather(self.active(), fresh_expert(), return_exceptions=True)
        self.assertIsInstance(expired, HTTPException)
        self.assert_deadline_rejection(expired)
        self.assertFalse(isinstance(fresh, BaseException), repr(fresh))
        self.assertEqual(message, fresh['message_id'])
