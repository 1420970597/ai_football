import asyncio
from contextlib import asynccontextmanager
import io
import json
import os
from pathlib import Path
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
from infra.remote.review_handoff import try_hold_idle_scheduler, pause_new_claims
from infra.remote import review_handoff


@unittest.skipUnless(os.environ.get("TEST_DATABASE_ADMIN_URL"), "requires isolated database")
class ReviewWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_setup_one_round_trip_initializes_new_pairs_without_resetting_turns(self):
        await self.pool.execute('INSERT INTO v2_review_source_schedule(source_id,last_turn) VALUES($1,17)', self.source)
        await self.pool.execute('INSERT INTO v2_review_expert_schedule(expert_id,last_turn,concurrency_limit) VALUES($1,19,3)', self.expert)
        source, expert = await self.add_expert()
        async with self.pool.acquire() as conn, conn.transaction():
            self.assertTrue(await worker._scheduler_lock(conn))
            calls = []
            class CountedConnection:
                async def fetchval(inner, query, *args):
                    calls.append(query)
                    return await conn.fetchval(query, *args)
            self.assertEqual(0, await worker.prepare_claim_schedule(
                CountedConnection(), worker.MAX_ATTEMPTS, worker.EXHAUSTED_BATCH_SIZE,
                worker.PERSISTENT_RETRY_FAILURES))
            self.assertEqual(1, len(calls), 'housekeeping must fit one remote round trip')
            self.assertEqual(0, await conn.fetchval('SELECT last_turn FROM v2_review_source_schedule WHERE source_id=$1', source))
            self.assertEqual(0, await conn.fetchval('SELECT last_turn FROM v2_review_expert_schedule WHERE expert_id=$1', expert))
            self.assertEqual(17, await conn.fetchval('SELECT last_turn FROM v2_review_source_schedule WHERE source_id=$1', self.source))
            row = await conn.fetchrow('SELECT last_turn,concurrency_limit FROM v2_review_expert_schedule WHERE expert_id=$1', self.expert)
            self.assertEqual((19, 3), tuple(row))

    async def test_claim_setup_rolls_back_expiry_and_metadata_together(self):
        await self.pool.execute("UPDATE v2_analysis_jobs SET attempts=$2,lease_until=now()-interval '1 second' WHERE id=$1", self.job, worker.MAX_ATTEMPTS)
        before = await self.pool.fetchrow('SELECT state,lease_until,last_error FROM v2_analysis_jobs WHERE id=$1', self.job)
        class AbortSetup(Exception):
            pass
        async with self.pool.acquire() as conn:
            with self.assertRaises(AbortSetup):
                async with conn.transaction():
                    self.assertTrue(await worker._scheduler_lock(conn))
                    await worker.prepare_claim_schedule(conn, worker.MAX_ATTEMPTS, worker.EXHAUSTED_BATCH_SIZE, worker.PERSISTENT_RETRY_FAILURES)
                    self.assertEqual('failed', await conn.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
                    raise AbortSetup()
        self.assertEqual(before, await self.pool.fetchrow('SELECT state,lease_until,last_error FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_source_schedule'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_expert_schedule'))

    async def test_claim_setup_expiry_batch_is_bounded_and_followup_sees_new_metadata(self):
        await self.pool.execute("UPDATE v2_analysis_jobs SET attempts=$2,lease_until=now()-interval '1 second' WHERE id=$1", self.job, worker.MAX_ATTEMPTS)
        other = (await self.queued())[0]
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='retry',attempts=$2 WHERE id=$1", other, worker.MAX_ATTEMPTS)
        source, expert = await self.add_expert()
        ready = (await self.queued(source=source, expert=expert))[0]
        async with self.pool.acquire() as conn, conn.transaction():
            self.assertTrue(await worker._scheduler_lock(conn))
            await worker.prepare_claim_schedule(conn, worker.MAX_ATTEMPTS, 1, worker.PERSISTENT_RETRY_FAILURES)
            self.assertEqual(1, await conn.fetchval("SELECT count(*) FROM v2_analysis_jobs WHERE state='failed'"))
        claimed = await worker.claim_job(self.pool)
        self.assertEqual(ready, claimed['id'])
        self.assertEqual('lease_exhausted', await self.pool.fetchval('SELECT last_error FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertEqual('retry_limit_without_transient_trace', await self.pool.fetchval('SELECT last_error FROM v2_analysis_jobs WHERE id=$1', other))

    async def test_handoff_hold_timeout_releases_lane_and_scheduler_and_closes_connection(self):
        await self.make_queued()
        release = Path('/tmp/e166-absent-'+uuid.uuid4().hex)
        settings = SimpleNamespace(database_url=self.admin_url.rsplit('/', 1)[0]+'/'+self.dbname)
        with patch.object(review_handoff, 'load_settings', return_value=settings):
            with self.assertRaises(TimeoutError):
                await review_handoff.run(release, .2, .05, pause_claims=True)
        self.assertEqual(0, await self.pool.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
            "AND application_name='review-idle-deployment-gate'"))
        self.assertIsNotNone(await worker.claim_job(self.pool))

    async def test_handoff_wait_timeout_releases_claim_pause_without_losing_lease(self):
        source, expert = await self.add_expert()
        ready = (await self.queued(source=source, expert=expert))[0]
        settings = SimpleNamespace(database_url=self.admin_url.rsplit('/', 1)[0]+'/'+self.dbname)
        with patch.object(review_handoff, 'load_settings', return_value=settings):
            with self.assertRaises(TimeoutError):
                await review_handoff.run(Path('/tmp/e166-absent-'+uuid.uuid4().hex), .05, .05, pause_claims=True)
        self.assertTrue(await worker._renew_lease(self.pool, {'id': self.job, 'attempt_id': self.attempt}))
        self.assertEqual(ready, (await worker.claim_job(self.pool))['id'])

    async def test_claim_pause_blocks_new_work_while_real_heartbeat_keeps_renewing(self):
        source, expert = await self.add_expert()
        ready = (await self.queued(source=source, expert=expert))[0]
        lease_until = await self.pool.fetchval(
            "UPDATE v2_analysis_jobs SET lease_until=clock_timestamp()+interval '10 seconds',heartbeat_at=clock_timestamp() "
            "WHERE id=$1 RETURNING lease_until", self.job)
        before = await self.pool.fetchval('SELECT heartbeat_at FROM v2_analysis_jobs WHERE id=$1', self.job)
        async with self.pool.acquire() as gate, gate.transaction():
            await pause_new_claims(gate)
            with patch.object(worker, 'HEARTBEAT_SECONDS', .1), patch.object(worker, 'LEASE_SECONDS', 20):
                heartbeat = asyncio.create_task(worker.keep_lease(self.pool, {
                    'id': self.job, 'attempt_id': self.attempt, 'lease_until': lease_until}))
                try:
                    for _ in range(2):
                        self.assertIsNone(await worker.claim_job(self.pool))
                    await asyncio.sleep(.3)
                    self.assertFalse(heartbeat.done(), 'handoff must preserve the active heartbeat')
                    row = await self.pool.fetchrow('SELECT heartbeat_at,lease_until FROM v2_analysis_jobs WHERE id=$1', self.job)
                    self.assertGreater(row['heartbeat_at'], before)
                    self.assertGreater(row['lease_until'], lease_until)
                    self.assertEqual('queued', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', ready))
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
        self.assertEqual(ready, (await worker.claim_job(self.pool))['id'])

    async def test_claim_pause_missing_lane_fails_without_retaining_locks(self):
        await self.pool.execute('DELETE FROM v2_review_lane_schedule')
        async with self.pool.acquire() as gate:
            with self.assertRaises(RuntimeError):
                await pause_new_claims(gate)
            with self.assertRaises(RuntimeError):
                async with gate.transaction():
                    await pause_new_claims(gate)
            self.assertFalse(gate.is_in_transaction())

    async def test_handoff_does_not_hold_scheduler_while_active_lease_needs_renewal(self):
        await self.pool.execute("UPDATE v2_analysis_jobs SET lease_until=now()+interval '15 seconds' WHERE id=$1", self.job)
        async with self.pool.acquire() as gate:
            self.assertFalse(await try_hold_idle_scheduler(gate))
            renewed = await worker._renew_lease(self.pool, {'id': self.job, 'attempt_id': self.attempt})
            self.assertGreater(renewed, self.now + timedelta(seconds=30))

    async def test_idle_handoff_blocks_claims_until_released(self):
        await self.make_queued()
        async with self.pool.acquire() as gate:
            self.assertTrue(await try_hold_idle_scheduler(gate))
            try:
                self.assertIsNone(await worker.claim_job(self.pool))
            finally:
                await gate.fetchval('SELECT pg_advisory_unlock(hashtext($1))', worker.SCHEDULER_LOCK)
        self.assertIsNotNone(await worker.claim_job(self.pool))

    async def test_handoff_inspection_failure_releases_its_session_lock(self):
        async with self.pool.acquire() as gate, self.pool.acquire() as other:
            class BrokenInspection:
                async def fetchval(self, query, *args):
                    if 'count(*)' in query:
                        raise TimeoutError('isolated inspection failure')
                    return await gate.fetchval(query, *args)
            with self.assertRaises(TimeoutError):
                await try_hold_idle_scheduler(BrokenInspection())
            acquired = await other.fetchval('SELECT pg_try_advisory_lock(hashtext($1))', worker.SCHEDULER_LOCK)
            self.assertTrue(acquired)
            await other.fetchval('SELECT pg_advisory_unlock(hashtext($1))', worker.SCHEDULER_LOCK)

    async def test_handoff_does_not_release_another_sessions_lock(self):
        async with self.pool.acquire() as gate, self.pool.acquire() as owner:
            await owner.fetchval('SELECT pg_advisory_lock(hashtext($1))', worker.SCHEDULER_LOCK)
            try:
                self.assertFalse(await try_hold_idle_scheduler(gate))
                self.assertFalse(await gate.fetchval('SELECT pg_try_advisory_lock(hashtext($1))', worker.SCHEDULER_LOCK))
            finally:
                await owner.fetchval('SELECT pg_advisory_unlock(hashtext($1))', worker.SCHEDULER_LOCK)

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

    async def seed_market_receipt(self, job, fixture, reason='refresh_timeout', *, attempt=None):
        attempt = attempt or job['attempt_id']
        await self.pool.execute(
            "INSERT INTO v2_review_fixture_bindings(job_id,attempt_id,fixture_id,search_key,evidence) "
            "VALUES($1,$2,$3,'synthetic-recovery','{}') ON CONFLICT DO NOTHING",
            job['id'], attempt, fixture)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await worker.trace(conn, job['id'], 'mcp_list_match_markets', {
                    'attempt_id': str(attempt), 'fixture_id': fixture, 'status': 'succeeded',
                    'refresh': {'attempted': True, 'accepted': reason == 'written', 'reason': reason}})

    async def finish_without_pick(self, job):
        with patch.object(worker, 'review_message', AsyncMock(return_value=('未录入', {'content': '未录入'}))), \
                patch.object(worker, 'delete_session', AsyncMock()):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model='synthetic'), job)

    async def test_transient_market_failure_retries_once_then_records_recovered_quote(self):
        fixture = await support.ReviewTests.fixture(self)
        await self.make_queued()
        first = await worker.claim_job(self.pool)
        await self.seed_market_receipt(first, fixture)
        await self.finish_without_pick(first)
        row = await self.pool.fetchrow('SELECT *,available_at>now()+interval \'15 seconds\' AS delayed FROM v2_analysis_jobs WHERE id=$1', self.job)
        self.assertEqual('retry', row['state'])
        self.assertEqual('temporary_market_refresh_failure', row['last_error'])
        self.assertTrue(row['delayed'])
        self.assertIsNone(await worker.claim_job(self.pool))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        await self.pool.execute("UPDATE v2_analysis_jobs SET available_at=now()-interval '1 second' WHERE id=$1", self.job)
        second = await worker.claim_job(self.pool)
        self.assertNotEqual(first['attempt_id'], second['attempt_id'])
        self.assertEqual(2, second['attempts'])
        await self.seed_market_receipt(second, fixture, 'written')

        async def recovered(*_args, **_kwargs):
            body = {'review_id': str(second['id']), 'attempt_id': str(second['attempt_id']), 'fixture_id': fixture}
            market = await self.client.post('/internal/mcp/list_match_markets', json=body)
            self.assertEqual(200, market.status_code)
            quote = market.json()['markets'][0]
            self.assertTrue(quote['can_record'])
            response = await self.client.post('/internal/mcp/record_expert_signals', json={
                'review_id': body['review_id'], 'attempt_id': body['attempt_id'],
                'signals': [{'fixture_id': fixture, 'quote_id': quote['quote_id'],
                             'confidence': '.9', 'evidence_text': 'Synthetic recovery intent'}]})
            self.assertEqual(200, response.status_code)
            self.assertEqual(1, len(response.json()['accepted']))
            return '已接受', {'content': '已接受'}

        with patch.object(worker, 'review_message', recovered), patch.object(worker, 'delete_session', AsyncMock()):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model='synthetic'), second)
        self.assertEqual('succeeded', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        for kind, expected in [('hermes_result', 2), ('system_review_result', 2), ('market_recovery_scheduled', 1)]:
            self.assertEqual(expected, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind=$2', self.job, kind))

    async def test_market_recovery_uses_latest_read_and_only_current_attempt(self):
        fixture = await support.ReviewTests.fixture(self)
        for mode in ('later_success', 'old_attempt'):
            with self.subTest(mode=mode):
                await self.make_queued()
                job = await worker.claim_job(self.pool)
                await self.seed_market_receipt(job, fixture, attempt=uuid.uuid4() if mode == 'old_attempt' else None)
                if mode == 'later_success':
                    await self.seed_market_receipt(job, fixture, 'written')
                await self.finish_without_pick(job)
                self.assertEqual('unresolved', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))

    async def test_market_recovery_does_not_retry_permanent_or_unconfigured_refresh(self):
        fixture = await support.ReviewTests.fixture(self)
        for reason in ('fixture_not_open', 'fixture_not_loaded', 'refresh_unconfigured', 'refresh_invalid_response', 'upstream_rate_limited'):
            with self.subTest(reason=reason):
                await self.make_queued()
                job = await worker.claim_job(self.pool)
                await self.seed_market_receipt(job, fixture, reason)
                await self.finish_without_pick(job)
                self.assertEqual('unresolved', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))

    async def test_market_recovery_stops_at_second_attempt_and_closed_fixture(self):
        fixture = await support.ReviewTests.fixture(self)
        for mode in ('attempt_limit', 'finished', 'cancelled', 'historical'):
            with self.subTest(mode=mode):
                await self.pool.execute("UPDATE v2_fixtures SET phase='scheduled' WHERE id=$1", fixture)
                await self.make_queued(attempts=1 if mode == 'attempt_limit' else 0)
                job = await worker.claim_job(self.pool)
                if mode == 'historical':
                    await self.pool.execute("UPDATE v2_analysis_jobs SET review_mode='historical_replay' WHERE id=$1", self.job)
                    job['review_mode'] = 'historical_replay'
                elif mode in ('finished', 'cancelled'):
                    await self.pool.execute('UPDATE v2_fixtures SET phase=$2 WHERE id=$1', fixture, mode)
                await self.seed_market_receipt(job, fixture)
                await self.finish_without_pick(job)
                self.assertEqual('unresolved', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))

    async def test_second_transient_failure_preserves_both_receipts_and_stops(self):
        fixture = await support.ReviewTests.fixture(self)
        await self.make_queued()
        for turn in (1, 2):
            await self.pool.execute("UPDATE v2_analysis_jobs SET available_at=now()-interval '1 second' WHERE id=$1", self.job)
            job = await worker.claim_job(self.pool)
            await self.seed_market_receipt(job, fixture, 'refresh_busy')
            await self.finish_without_pick(job)
            self.assertEqual('retry' if turn == 1 else 'unresolved', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertIsNone(await worker.claim_job(self.pool))
        self.assertEqual(2, await self.pool.fetchval("SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='system_review_result'", self.job))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_explicit_selected_quote_stale_refusal_retries_without_price_bypass(self):
        fixture = await support.ReviewTests.fixture(self)
        quote = await self.pool.fetchval('SELECT id FROM v2_current_quotes WHERE fixture_id=$1', fixture)
        for reason in ('quote_not_read_or_changed', 'quote_or_score_context_changed', 'quote_inactive_unknown_or_stale'):
            with self.subTest(reason=reason):
                await self.make_queued()
                job = await worker.claim_job(self.pool)
                await self.seed_market_receipt(job, fixture, 'written')
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        await worker.trace(conn, job['id'], 'mcp_record_expert_signals', {
                            'attempt_id': str(job['attempt_id']), 'status': 'succeeded',
                            'parameters': {'signals': [{'fixture_id': fixture, 'quote_id': str(quote)}]},
                            'accepted': [], 'rejected': [{'index': 0, 'reason': reason}]})
                await self.finish_without_pick(job)
                self.assertEqual('retry' if reason == 'quote_inactive_unknown_or_stale' else 'unresolved',
                                 await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_existing_signal_is_preserved_despite_later_temporary_read_failure(self):
        fixture = await support.ReviewTests.fixture(self)
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        await self.seed_market_receipt(job, fixture, 'written')
        body = {'review_id': str(job['id']), 'attempt_id': str(job['attempt_id']), 'fixture_id': fixture}
        market = await self.client.post('/internal/mcp/list_match_markets', json=body)
        self.assertEqual(200, market.status_code)
        quote = market.json()['markets'][0]
        response = await self.client.post('/internal/mcp/record_expert_signals', json={
            'review_id': body['review_id'], 'attempt_id': body['attempt_id'],
            'signals': [{'fixture_id': fixture, 'quote_id': quote['quote_id'], 'evidence_text': 'Synthetic accepted quote'}]})
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(response.json()['accepted']))
        before = await self.pool.fetchrow('SELECT * FROM v2_expert_signals')
        await self.seed_market_receipt(job, fixture, 'refresh_timeout')
        await self.finish_without_pick(job)
        self.assertEqual('succeeded', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertEqual(dict(before), dict(await self.pool.fetchrow('SELECT * FROM v2_expert_signals')))
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='market_recovery_scheduled'"))

    async def test_partial_multi_match_recovery_preserves_already_accepted_signal(self):
        first_fixture = await support.ReviewTests.fixture(self)
        second_fixture = await support.ReviewTests.fixture(self, index=2)
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        await self.seed_market_receipt(job, first_fixture, 'written')
        context = {'review_id': str(job['id']), 'attempt_id': str(job['attempt_id'])}
        market = await self.client.post('/internal/mcp/list_match_markets', json={**context, 'fixture_id': first_fixture})
        self.assertEqual(200, market.status_code)
        response = await self.client.post('/internal/mcp/record_expert_signals', json={**context, 'signals': [{
            'fixture_id': first_fixture, 'quote_id': market.json()['markets'][0]['quote_id'],
            'evidence_text': 'Synthetic first accepted leg'}]})
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(response.json()['accepted']))
        before = dict(await self.pool.fetchrow('SELECT * FROM v2_expert_signals'))
        await self.seed_market_receipt(job, first_fixture, 'refresh_timeout')
        await self.seed_market_receipt(job, second_fixture, 'refresh_timeout')
        await self.finish_without_pick(job)
        self.assertEqual('retry', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
        recovery = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='market_recovery_scheduled'")
        self.assertEqual([{'fixture_id': second_fixture, 'reason': 'refresh_timeout'}], recovery['fixtures'])
        self.assertEqual(before, dict(await self.pool.fetchrow('SELECT * FROM v2_expert_signals')))
        receipt = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertEqual('retry', receipt['outcome'])
        self.assertEqual(1, receipt['accepted_count'])

    async def test_partial_same_match_different_lines_recovers_only_unaccepted_option(self):
        markets = [{'market_id': 'total', 'market_name': '全场大小', 'kind': 'total', 'period': 'full_time',
                    'line': line, 'selection': 'over', 'price': '1.95',
                    'source_updated_at': self.now.isoformat(), 'received_at': self.now.isoformat()}
                   for line in ('2.5', '2.75')]
        fixture = await support.ReviewTests.fixture(self, markets=markets)
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        await self.seed_market_receipt(job, fixture, 'written')
        context = {'review_id': str(job['id']), 'attempt_id': str(job['attempt_id'])}
        market = await self.client.post('/internal/mcp/list_match_markets', json={**context, 'fixture_id': fixture})
        self.assertEqual(200, market.status_code)
        quotes = market.json()['markets']
        self.assertEqual(2, len(quotes))
        accepted = await self.client.post('/internal/mcp/record_expert_signals', json={**context, 'signals': [{
            'fixture_id': fixture, 'quote_id': quotes[0]['quote_id'], 'evidence_text': 'Synthetic first line'}]})
        self.assertEqual(200, accepted.status_code)
        self.assertEqual(1, len(accepted.json()['accepted']))
        before = dict(await self.pool.fetchrow('SELECT * FROM v2_expert_signals'))
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await worker.trace(conn, job['id'], 'mcp_record_expert_signals', {
                    'attempt_id': str(job['attempt_id']), 'status': 'succeeded',
                    'parameters': {'signals': [{'fixture_id': fixture, 'quote_id': q['quote_id']} for q in quotes]},
                    'accepted': [], 'rejected': [{'index': i, 'reason': 'quote_inactive_unknown_or_stale'} for i in (0, 1)]})
        await self.finish_without_pick(job)
        self.assertEqual('retry', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertEqual(before, dict(await self.pool.fetchrow('SELECT * FROM v2_expert_signals')))
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='market_recovery_scheduled'"))

    async def test_claim_preserves_current_and_legacy_reply_snapshot_text(self):
        for context, expected in [({'text': '珀斯光荣 阿德莱德 全场大0.75'}, '珀斯光荣 阿德莱德 全场大0.75'),
                                  ({'raw_text': '旧格式全场大2.5'}, '旧格式全场大2.5'),
                                  ({'text': '当前快照', 'raw_text': '旧快照'}, '当前快照'), ({}, None)]:
            with self.subTest(context=context):
                await self.pool.execute('UPDATE v2_expert_messages SET reply_context=$2,reply_to_platform_message_id=$3 WHERE id=$1',
                                        self.message, context, 'not-in-local-archive')
                await self.make_queued()
                job = await worker.claim_job(self.pool)
                self.assertIsNotNone(job)
                self.assertEqual(expected, job['message']['reply_text'])

    async def test_claim_recovers_same_source_reply_image_and_album_member_without_deleted_fallback(self):
        parent = uuid.uuid4()
        image = {'object_key': 'v2/tg/reference.png', 'mime_type': 'image/png', 'bytes': 123, 'sha256': 'a'*64}
        await self.pool.execute(
            "INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at,media_objects,media_state) "
            "VALUES($1,$2,$3,'album:trusted-reply','图中哈尔迪亚',now(),$4,'ready')",
            parent, self.source, self.expert, [image])
        event = uuid.uuid4()
        await self.pool.execute(
            "INSERT INTO v2_telegram_events(id,source_id,platform_message_id,version_key,source_version_at,event_kind,content,media_state,message_id) "
            "VALUES($1,$2,'album:trusted-reply','reference-key',now(),'message','{}','ready',$3)", event, self.source, parent)
        await self.pool.execute('INSERT INTO v2_telegram_event_members(event_id,platform_message_id) VALUES($1,20318)', event)
        for reference in ('album:trusted-reply', '20318'):
            await self.pool.execute('UPDATE v2_expert_messages SET reply_to_platform_message_id=$2 WHERE id=$1', self.message, reference)
            await self.make_queued()
            job = await worker.claim_job(self.pool)
            self.assertEqual('图中哈尔迪亚', job['message']['reply_text'])
            self.assertEqual(str(parent), job['message']['reply_archive']['id'])
            self.assertEqual([image], job['message']['reply_archive']['media_objects'])
        await self.pool.execute('UPDATE v2_expert_messages SET deleted_at=now() WHERE id=$1', parent)
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        self.assertTrue(job['message']['reply_archive']['deleted'])
        self.assertEqual([], job['message']['reply_archive']['media_objects'])
        other_source, other_expert = await self.add_expert()
        await self.pool.execute("UPDATE v2_expert_messages SET source_id=$2,expert_id=$3,deleted_at=NULL WHERE id=$1",
                                parent, other_source, other_expert)
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        self.assertIsNone(job['message']['reply_archive'], 'source isolation must apply to album member lookup')

    async def test_reference_image_edit_or_deletion_invalidates_attempt_tool_access(self):
        from app.review_state import active_job
        from fastapi import HTTPException
        parent = uuid.uuid4()
        image = {'object_key': 'v2/tg/reference.png', 'mime_type': 'image/png'}
        await self.pool.execute(
            "INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at,media_objects,media_state) "
            "VALUES($1,$2,$3,'reference-image','原图',now(),$4,'ready')", parent, self.source, self.expert, [image])
        await self.pool.execute('UPDATE v2_expert_messages SET reply_to_platform_message_id=$2 WHERE id=$1',
                                self.message, 'reference-image')
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        await worker._add_trace(self.pool, job, 'hermes_request', {'reply_archive': job['message']['reply_archive']})
        async with self.pool.acquire() as conn:
            self.assertIsNotNone(await active_job(conn, job['id'], job['attempt_id']))
        for operation in ('delete', 'new_revision'):
            with self.subTest(operation=operation):
                if operation == 'delete':
                    await self.pool.execute('UPDATE v2_expert_messages SET deleted_at=now() WHERE id=$1', parent)
                else:
                    await self.pool.execute('UPDATE v2_expert_messages SET deleted_at=NULL WHERE id=$1', parent)
                    await self.pool.execute(
                        "INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at,revision) "
                        "VALUES($1,$2,$3,'reference-image','修订',now(),2)", uuid.uuid4(), self.source, self.expert)
                async with self.pool.acquire() as conn:
                    with self.assertRaises(HTTPException) as raised:
                        await active_job(conn, job['id'], job['attempt_id'])
                    self.assertEqual(409, raised.exception.status_code)
                    self.assertIn('referenced image', raised.exception.detail)

    async def test_local_latest_reply_precedes_snapshot_and_other_source_is_isolated(self):
        for revision in (1, 2):
            await self.pool.execute(
                'INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at,revision) '
                'VALUES($1,$2,$3,$4,$5,now(),$6)', uuid.uuid4(), self.source, self.expert, 'local-reply',
                f'本地版本{revision}', revision)
        other_source, other_expert = await self.add_expert()
        await self.pool.execute(
            'INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) '
            'VALUES($1,$2,$3,$4,$5,now())', uuid.uuid4(), other_source, other_expert, 'cross-source-only', '不可引用其他群')
        for reply_id, expected in [('local-reply', '本地版本2'), ('cross-source-only', '当前群回复快照')]:
            await self.pool.execute('UPDATE v2_expert_messages SET reply_to_platform_message_id=$2,reply_context=$3 WHERE id=$1',
                                    self.message, reply_id, {'text': '当前群回复快照'})
            await self.make_queued()
            self.assertEqual(expected, (await worker.claim_job(self.pool))['message']['reply_text'])

    async def test_recovered_reply_reaches_hermes_and_immutable_request_trace(self):
        reply = '原始回复：Alpha Beta 全场大2.5'
        await self.pool.execute('UPDATE v2_expert_messages SET reply_context=$2,reply_to_platform_message_id=$3 WHERE id=$1',
                                self.message, {'text': reply}, 'remote-only-reply')
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        model = AsyncMock(return_value=('未录入', {'content': '未录入'}))
        with patch.object(worker, 'review_message', model), patch.object(worker, 'delete_session', AsyncMock()):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model='synthetic'), job)
        self.assertEqual(reply, model.call_args.args[1]['reply_text'])
        payload = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='hermes_request'", self.job)
        self.assertEqual(reply, payload['reply_text'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_only_one_worker_claims_and_attempt_changes_after_expiry(self):
        await self.make_queued()
        first, second = await asyncio.gather(worker.claim_job(self.pool), worker.claim_job(self.pool))
        self.assertEqual(1, sum(item is not None for item in (first, second)))
        claimed = first or second
        await self.pool.execute("UPDATE v2_analysis_jobs SET lease_until=now()-interval '1 second' WHERE id=$1", self.job)
        retry = await worker.claim_job(self.pool)
        self.assertNotEqual(claimed["attempt_id"], retry["attempt_id"])
        self.assertEqual(2, retry["attempts"])

    async def test_queue_notifications_are_commit_scoped_and_contain_no_payload(self):
        listener = await asyncpg.connect(self.admin_url.rsplit('/', 1)[0] + '/' + self.dbname)
        notified, payloads = asyncio.Event(), []
        await listener.add_listener(worker.JOB_READY_CHANNEL,
                                    lambda _conn, _pid, _channel, payload: (payloads.append(payload), notified.set()))
        try:
            await self.complete({'id': self.job})
            async with self.pool.acquire() as conn:
                with self.assertRaisesRegex(RuntimeError, 'rollback fixture'):
                    async with conn.transaction():
                        message_id, job_id = uuid.uuid4(), uuid.uuid4()
                        await conn.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) "
                                           "VALUES($1,$2,$3,$4,'SYNTHETIC rollback test',now())",
                                           message_id, self.source, self.expert, str(message_id))
                        await conn.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,available_at) "
                                           "VALUES($1,$2,'queued',$3,now())", job_id, message_id, str(job_id))
                        raise RuntimeError('rollback fixture')
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(notified.wait(), timeout=.05)

                message_id, job_id = uuid.uuid4(), uuid.uuid4()
                async with conn.transaction():
                    await conn.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) "
                                       "VALUES($1,$2,$3,$4,'SYNTHETIC commit test',now())",
                                       message_id, self.source, self.expert, str(message_id))
                    await conn.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,available_at) "
                                       "VALUES($1,$2,'queued',$3,now())", job_id, message_id, str(job_id))
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(notified.wait(), timeout=.05)
            await asyncio.wait_for(notified.wait(), timeout=1)
            self.assertEqual([''], payloads)
            self.assertEqual(job_id, (await worker.claim_job(self.pool))['id'])
        finally:
            await listener.close()

    async def test_claim_allows_slow_remote_roundtrips_but_still_has_a_deadline(self):
        await self.make_queued()
        real_claim = worker._claim_job
        async def slow_claim(pool):
            await asyncio.sleep(.06)
            return await real_claim(pool)
        with patch.object(worker, 'CLAIM_TIMEOUT_SECONDS', .5), patch.object(worker, '_claim_job', slow_claim):
            self.assertEqual(self.job, (await worker.claim_job(self.pool))['id'])
        cancelled = asyncio.Event()
        async def stalled(_pool):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        with patch.object(worker, 'CLAIM_TIMEOUT_SECONDS', .02), patch.object(worker, '_claim_job', stalled):
            self.assertIsNone(await worker.claim_job(self.pool))
        self.assertTrue(cancelled.is_set())

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
        conclusion = await self.pool.fetchrow(
            "SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='system_review_result'", self.job)
        self.assertEqual("unresolved", conclusion["payload"]["outcome"])
        self.assertEqual(0, conclusion["payload"]["signal_count"])
        self.assertGreaterEqual(conclusion["payload"]["event_to_conclusion_ms"],
                                conclusion["payload"]["claim_to_conclusion_ms"])

    async def test_heartbeat_finishes_before_model_result_and_conclusion_writes(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        heartbeat_stopped = asyncio.Event()
        original_add_trace = worker._add_trace

        async def heartbeat(*_args):
            try:
                await asyncio.Future()
            finally:
                heartbeat_stopped.set()

        async def add_trace(pool, active, kind, payload):
            if kind == 'hermes_result':
                self.assertTrue(heartbeat_stopped.is_set())
            return await original_add_trace(pool, active, kind, payload)

        with patch.object(worker, 'keep_lease', heartbeat), \
                patch.object(worker, '_add_trace', add_trace), \
                patch.object(worker, 'review_message', AsyncMock(return_value=('no pick', {'content': 'no pick'}))), \
                patch.object(worker, 'delete_session', AsyncMock()):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model='synthetic'), job)

        self.assertTrue(heartbeat_stopped.is_set())
        self.assertEqual('unresolved', await self.pool.fetchval(
            'SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertEqual(1, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='system_review_result'",
            self.job))

    async def test_conclusion_rollback_retries_without_repeating_model_or_receipt(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        original = worker.record_summary
        calls = 0

        async def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = await original(*args, **kwargs)
            if calls == 1:
                raise TimeoutError('transaction must roll back its receipt')
            return result

        model = AsyncMock(return_value=('no pick', {'content': 'no pick'}))
        with patch.object(worker, 'record_summary', interrupted), \
                patch.object(worker, 'review_message', model), \
                patch.object(worker, 'delete_session', AsyncMock()):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model='synthetic'), job)
        model.assert_awaited_once()
        self.assertEqual(2, calls)
        self.assertEqual('unresolved', await self.pool.fetchval(
            'SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertEqual(1, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='system_review_result'", self.job))
        self.assertEqual(1, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='hermes_result'", self.job))

    async def test_committed_conclusion_with_lost_acknowledgement_is_not_written_twice(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        original = worker._complete_review_once
        complete = AsyncMock()

        async def lost_ack(*args):
            await complete()
            await original(*args)
            raise asyncpg.ConnectionDoesNotExistError('commit acknowledgement lost')

        model = AsyncMock(return_value=('no pick', {'content': 'no pick'}))
        with patch.object(worker, '_complete_review_once', lost_ack), \
                patch.object(worker, 'review_message', model), \
                patch.object(worker, 'delete_session', AsyncMock()):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model='synthetic'), job)
        model.assert_awaited_once()
        complete.assert_awaited_once()
        self.assertEqual('unresolved', await self.pool.fetchval(
            'SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertEqual(1, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='system_review_result'", self.job))
        self.assertEqual(0, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='review_failure'", self.job))

    async def test_conclusion_recovery_is_bounded_and_keeps_original_failure_policy(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        conclude = AsyncMock(side_effect=TimeoutError())
        model = AsyncMock(return_value=('no pick', {'content': 'no pick'}))
        with patch.object(worker, '_complete_review_once', conclude), \
                patch.object(worker, 'review_message', model), \
                patch.object(worker, 'delete_session', AsyncMock()):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model='synthetic'), job)
        self.assertEqual(2, conclude.await_count)
        model.assert_awaited_once()
        self.assertEqual('retry', await self.pool.fetchval(
            'SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
        failure = await self.pool.fetchval(
            "SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='review_failure'", self.job)
        self.assertEqual('conclusion_persistence', failure['failure_stage'])
        self.assertEqual(0, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='system_review_result'", self.job))

    async def test_conclusion_recovery_rechecks_source_eligibility_before_retry(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        original = worker._complete_review_once
        calls = 0

        async def source_disabled(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                await self.pool.execute('UPDATE v2_expert_sources SET enabled=false')
                raise TimeoutError()
            return await original(*args)

        with patch.object(worker, '_complete_review_once', source_disabled):
            with self.assertRaises(worker.HTTPException) as failed:
                await worker._complete_review(self.pool, job, worker.time.monotonic())
        self.assertEqual(409, failed.exception.status_code)
        self.assertEqual(2, calls)
        self.assertEqual(0, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='system_review_result'"))

    async def test_conclusion_receipt_verification_rejects_old_attempt(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        await worker._complete_review_once(self.pool, job, worker.time.monotonic())
        self.assertTrue(await worker._conclusion_committed(self.pool, job))
        await self.pool.execute('UPDATE v2_analysis_jobs SET attempt_id=$1 WHERE id=$2', uuid.uuid4(), self.job)
        self.assertFalse(await worker._conclusion_committed(self.pool, job))

    async def test_conclusion_reuses_eligibility_trace_lock_for_receipt_and_recovery(self):
        for recovering in (False, True):
            with self.subTest(recovering=recovering):
                await self.make_queued()
                job = await worker.claim_job(self.pool)
                if recovering:
                    fixture = await support.ReviewTests.fixture(self)
                    await self.seed_market_receipt(job, fixture)
                queries = []
                pool = self.pool

                class CountedConnection:
                    def __init__(inner, conn):
                        inner.conn = conn

                    def __getattr__(inner, name):
                        target = getattr(inner.conn, name)
                        if name not in {'fetch', 'fetchrow', 'fetchval', 'execute'}:
                            return target

                        async def counted(query, *args):
                            queries.append(query)
                            return await target(query, *args)
                        return counted

                class CountedPool:
                    @asynccontextmanager
                    async def acquire(inner):
                        async with pool.acquire() as conn:
                            yield CountedConnection(conn)

                await worker._complete_review_once(CountedPool(), job, worker.time.monotonic())
                self.assertEqual(7 if recovering else 6, len(queries))
                self.assertIn('v2-review-trace:', queries[0])
                self.assertFalse(any(query.startswith('SELECT pg_advisory_xact_lock') for query in queries))
                self.assertEqual('retry' if recovering else 'unresolved', await self.pool.fetchval(
                    'SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
                self.assertEqual(1, await self.pool.fetchval(
                    "SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='system_review_result' "
                    "AND payload->>'attempt_id'=$2", self.job, str(job['attempt_id'])))
                self.assertEqual(int(recovering), await self.pool.fetchval(
                    "SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='market_recovery_scheduled' "
                    "AND payload->>'attempt_id'=$2", self.job, str(job['attempt_id'])))

    async def test_conclusion_waits_for_concurrent_trace_and_reads_committed_sequence(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        await worker._add_trace(self.pool, job, 'hermes_result', {'content': 'no pick'})
        completion = None
        try:
            async with self.pool.acquire() as holder, holder.transaction():
                await worker.trace(holder, self.job, 'parallel_tool_receipt', {
                    'attempt_id': str(job['attempt_id']), 'status': 'succeeded'})
                completion = asyncio.create_task(
                    worker._complete_review_once(self.pool, job, worker.time.monotonic()))
                for _ in range(100):
                    waiting = await self.pool.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                        "AND pid<>pg_backend_pid() AND wait_event_type='Lock' "
                        "AND position('AS trace_lock' in query)>0)")
                    if waiting:
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(waiting, 'completion must wait on the existing trace lock')
                self.assertFalse(completion.done())
            await asyncio.wait_for(completion, 5)
        finally:
            if completion is not None and not completion.done():
                completion.cancel()
                await asyncio.gather(completion, return_exceptions=True)
        rows = await self.pool.fetch(
            'SELECT sequence_no,trace_kind FROM v2_analysis_traces WHERE job_id=$1 ORDER BY sequence_no', self.job)
        self.assertEqual(list(range(1, len(rows) + 1)), [row['sequence_no'] for row in rows])
        order = {row['trace_kind']: row['sequence_no'] for row in rows}
        self.assertLess(order['parallel_tool_receipt'], order['system_review_result'])
        self.assertEqual(1, sum(row['trace_kind'] == 'hermes_result' for row in rows))
        self.assertEqual(1, sum(row['trace_kind'] == 'system_review_result' for row in rows))
        self.assertEqual('unresolved', await self.pool.fetchval(
            'SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))

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

    async def test_provider_error_is_delayed_retry_without_false_result_or_receipt(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        with (
                patch.object(worker, "review_message", AsyncMock(side_effect=worker.HermesProviderUnavailable())),
                patch.object(worker, "delete_session", AsyncMock()),
                patch.object(worker.random, "randint", return_value=0),
        ):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model="hermes-agent"), job)
        row = await self.pool.fetchrow("SELECT state,last_error,lease_until,available_at>now()+interval '59 seconds' AS delayed "
                                       "FROM v2_analysis_jobs WHERE id=$1", self.job)
        self.assertEqual("retry", row["state"])
        self.assertEqual("HermesProviderUnavailable", row["last_error"])
        self.assertTrue(row["delayed"])
        self.assertIsNone(row["lease_until"])
        failure = await self.pool.fetchrow(
            "SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='review_failure'", self.job)
        self.assertEqual("provider_transient", failure["payload"]["failure_category"])
        self.assertTrue(failure["payload"]["retryable"])
        self.assertEqual(0, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind IN ('hermes_result','system_review_result')",
            self.job))

    async def test_provider_transient_remains_claimable_after_attempt_limit(self):
        await self.make_queued()
        job = await worker.claim_job(self.pool)
        await self.pool.execute('UPDATE v2_analysis_jobs SET attempts=$2 WHERE id=$1', self.job, worker.MAX_ATTEMPTS)
        job['attempts'] = worker.MAX_ATTEMPTS
        with (
                patch.object(worker, "review_message", AsyncMock(side_effect=worker.HermesProviderUnavailable())),
                patch.object(worker, "delete_session", AsyncMock()),
                patch.object(worker.random, "randint", return_value=0),
        ):
            await worker.process_job(self.pool, SimpleNamespace(hermes_model="hermes-agent"), job)
        row = await self.pool.fetchrow(
            "SELECT state,last_error,attempts,available_at>now()+interval '899 seconds' AS delayed "
            "FROM v2_analysis_jobs WHERE id=$1", self.job)
        self.assertEqual(('retry', 'HermesProviderUnavailable', worker.MAX_ATTEMPTS, True),
                         (row['state'], row['last_error'], row['attempts'], row['delayed']))
        failure = await self.pool.fetchrow(
            "SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='review_failure'", self.job)
        self.assertEqual(str(job['attempt_id']), failure['payload']['attempt_id'])
        self.assertEqual('provider_transient', failure['payload']['failure_category'])
        self.assertEqual(0, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1 "
            "AND trace_kind IN ('hermes_result','system_review_result')", self.job))
        await self.pool.execute("UPDATE v2_analysis_jobs SET available_at=now()-interval '1 second' WHERE id=$1", self.job)
        next_attempt = await worker.claim_job(self.pool)
        self.assertEqual(worker.MAX_ATTEMPTS + 1, next_attempt['attempts'])

    async def test_attempt_limit_cannot_be_bypassed_without_matching_transient_trace(self):
        await self.make_queued(attempts=worker.MAX_ATTEMPTS)
        await self.pool.execute(
            "UPDATE v2_analysis_jobs SET state='retry',attempt_id=$2,last_error='HermesProviderUnavailable' WHERE id=$1",
            self.job, uuid.uuid4())
        self.assertIsNone(await worker.claim_job(self.pool))
        row = await self.pool.fetchrow('SELECT state,last_error FROM v2_analysis_jobs WHERE id=$1', self.job)
        self.assertEqual(('failed', 'retry_limit_without_transient_trace'), (row['state'], row['last_error']))

    async def test_terminal_provider_failure_with_matching_attempt_trace_can_recover(self):
        await self.make_queued(attempts=worker.MAX_ATTEMPTS)
        attempt_id = uuid.uuid4()
        await self.pool.execute(
            "UPDATE v2_analysis_jobs SET state='failed',attempt_id=$2,review_mode='real_time',"
            "last_error='HermesProviderUnavailable',available_at=now()-interval '1 second' WHERE id=$1",
            self.job, attempt_id)
        async with self.pool.acquire() as conn:
            await worker.trace(conn, self.job, 'review_failure', {
                'attempt_id': str(attempt_id), 'failure_category': 'provider_transient',
                'retryable': True, 'failure_stage': 'model_review',
            })
        recovered = await worker.claim_job(self.pool)
        self.assertEqual(self.job, recovered['id'])
        self.assertEqual(worker.MAX_ATTEMPTS + 1, recovered['attempts'])

    async def test_terminal_historical_provider_failure_stays_closed(self):
        await self.make_queued(attempts=worker.MAX_ATTEMPTS)
        attempt_id = uuid.uuid4()
        await self.pool.execute(
            "UPDATE v2_analysis_jobs SET state='retry',attempt_id=$2,review_mode='historical_replay',"
            "last_error='HermesProviderUnavailable',available_at=now()-interval '1 second' WHERE id=$1",
            self.job, attempt_id)
        async with self.pool.acquire() as conn:
            await worker.trace(conn, self.job, 'review_failure', {
                'attempt_id': str(attempt_id), 'failure_category': 'provider_transient',
                'retryable': True, 'failure_stage': 'model_review',
            })
        self.assertIsNone(await worker.claim_job(self.pool))
        row = await self.pool.fetchrow('SELECT state,last_error FROM v2_analysis_jobs WHERE id=$1', self.job)
        self.assertEqual(('failed', 'retry_limit_without_transient_trace'), (row['state'], row['last_error']))

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
        self.assertIsNone(await worker.claim_job(self.pool))

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

    async def test_expert_concurrency_floor_uses_existing_worker_capacity(self):
        await self.make_queued()
        await self.queued(count=3)
        with patch.object(worker, 'EXPERT_CONCURRENCY_FLOOR', 2):
            first = await worker.claim_job(self.pool)
            second = await worker.claim_job(self.pool)
            third = await worker.claim_job(self.pool)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(third)
        self.assertEqual(2, await self.pool.fetchval(
            "SELECT count(*) FROM v2_analysis_jobs WHERE expert_id=$1 AND state='running' "
            "AND lease_until>clock_timestamp()", self.expert))

    async def test_expert_concurrency_floor_defaults_to_existing_database_limit(self):
        await self.make_queued()
        await self.queued(count=2)
        with patch.object(worker, 'EXPERT_CONCURRENCY_FLOOR', 1):
            first = await worker.claim_job(self.pool)
            second = await worker.claim_job(self.pool)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

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
