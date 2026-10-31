from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
import random
import time
import uuid

import httpx
from . import database
from .config import load_settings
from .hermes_client import delete_session, resolve_review_settings, review_message
from .review_state import active_job, trace
from .review_summary import record_summary
from .review_policy import REALTIME_WINDOW_SECONDS
from fastapi import HTTPException
import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("platform.review-worker")
CONCURRENCY = max(1, min(8, int(os.environ.get("REVIEW_WORKER_CONCURRENCY", "2"))))
MAX_ATTEMPTS = 5
LEASE_SECONDS = 90
HEARTBEAT_SECONDS = 20
REVIEW_TIMEOUT = max(60, min(1800, int(os.environ.get("REVIEW_TIMEOUT_SECONDS", "600"))))
# The scheduler performs multiple remote PostgreSQL round trips in a single
# transaction. Five seconds repeatedly cancelled otherwise healthy claims on
# the measured remote link. Keep an overall deadline as well as the per-query
# lock/statement timeouts below so a stalled database still cannot hang a worker.
CLAIM_TIMEOUT_SECONDS = 30
SCHEDULER_LOCK = 'platform-v2-review-fair-scheduler'
MAX_PAIR_PROBES = 8
EXHAUSTED_BATCH_SIZE = 100
# Posting age controls audit scheduling, not whether a match is prematch/live.
# Historical replay preserves the original text and may not create live signals.
REALTIME_CLAIMS_PER_REPLAY = max(1, min(1000, int(os.environ.get('REVIEW_REALTIME_CLAIMS_PER_REPLAY', '8'))))
HISTORICAL_CONCURRENCY = max(0, min(8, int(os.environ.get('REVIEW_HISTORICAL_CONCURRENCY', '1'))))
JOB_READY_CHANNEL = 'v2_analysis_job_ready'
ELIGIBLE_MESSAGE = (
    "m.deleted_at IS NULL AND m.media_state='ready' AND NOT EXISTS(SELECT 1 FROM v2_expert_messages newer "
    "WHERE newer.source_id=m.source_id AND newer.platform_message_id=m.platform_message_id AND newer.revision>m.revision)"
)


class LeaseLost(RuntimeError):
    pass


def failure_category(exc: BaseException) -> tuple[str, bool]:
    """Return a bounded, non-sensitive failure class and retry policy."""
    if isinstance(exc, LeaseLost):
        return "lease_lost", True
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return "timeout", True
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return "network", True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            return "provider_auth", False
        if status in {408, 425, 429} or status >= 500:
            return "provider_transient", True
        return "provider_rejected", False
    message = str(exc).lower()
    if any(value in message for value in ('unsupported telegram image', 'image type', 'image total bytes',
            'remote image', 'review image count', 'telegram image exceeds')):
        return "visual_input_invalid", False
    if "content_filter" in message or "did not finish" in message:
        return "model_rejected", False
    if "no assistant message" in message or "unsupported result shape" in message:
        return "model_invalid_response", True
    return "internal", True


async def _scheduler_lock(conn):
    await conn.execute("SELECT set_config('lock_timeout','2000',true),set_config('statement_timeout','3000',true)")
    return await conn.fetchval('SELECT pg_try_advisory_xact_lock(hashtext($1))', SCHEDULER_LOCK)


async def _advance_turn(conn, source_id, expert_id):
    turn = await conn.fetchval(
        'SELECT GREATEST(COALESCE((SELECT max(last_turn) FROM v2_review_source_schedule),0),'
        'COALESCE((SELECT max(last_turn) FROM v2_review_expert_schedule),0))+1')
    await conn.execute('UPDATE v2_review_source_schedule SET last_turn=$2 WHERE source_id=$1', source_id, turn)
    await conn.execute('UPDATE v2_review_expert_schedule SET last_turn=$2 WHERE expert_id=$1', expert_id, turn)
    return turn


def _lane_condition(mode):
    bound = "statement_timestamp()-($2::int*interval '1 second')"
    if mode == 'real_time':
        return 'j.message_posted_at>=' + bound + ' AND j.message_posted_at<=statement_timestamp()'
    return 'j.message_posted_at<' + bound


async def _claim_from_lane(conn, mode):
    lane = _lane_condition(mode)
    for probe in range(MAX_PAIR_PROBES):
        pair = await conn.fetchrow(
            "SELECT ss.source_id,es.expert_id,es.concurrency_limit,"
            "(SELECT count(*) FROM v2_analysis_jobs running WHERE running.expert_id=es.expert_id "
            "AND running.state='running' AND running.lease_until>statement_timestamp()) AS active_leases "
            "FROM v2_review_source_schedule ss JOIN v2_expert_sources s ON s.id=ss.source_id "
            "JOIN v2_source_experts se ON se.source_id=s.id JOIN v2_experts e ON e.id=se.expert_id "
            "JOIN v2_review_expert_schedule es ON es.expert_id=e.id "
            "WHERE s.enabled AND e.enabled AND se.enabled AND (SELECT count(*) FROM v2_analysis_jobs running "
            "WHERE running.expert_id=es.expert_id AND running.state='running' AND running.lease_until>statement_timestamp())<es.concurrency_limit "
            "AND EXISTS(SELECT 1 FROM v2_analysis_jobs j JOIN v2_expert_messages m ON m.id=j.message_id "
            "WHERE j.source_id=ss.source_id AND j.expert_id=es.expert_id AND j.attempts<$1 AND " + lane +
            " AND ((j.state IN('queued','retry') AND j.available_at<=statement_timestamp()) OR "
            "(j.state='running' AND (j.lease_until IS NULL OR j.lease_until<=statement_timestamp()))) AND " + ELIGIBLE_MESSAGE + ") "
            "ORDER BY ss.last_turn,es.last_turn,ss.source_id,es.expert_id LIMIT 1 FOR SHARE OF s,e,se FOR UPDATE OF ss,es",
            MAX_ATTEMPTS, REALTIME_WINDOW_SECONDS)
        if not pair:
            return None
        turn = await _advance_turn(conn, pair['source_id'], pair['expert_id'])
        for expired in (True, False):
            condition = ("j.state='running' AND (j.lease_until IS NULL OR j.lease_until<=statement_timestamp())" if expired
                         else "j.state IN('queued','retry') AND j.available_at<=statement_timestamp()")
            order = 'j.lease_until NULLS FIRST,j.created_at,j.id' if expired else 'j.message_posted_at DESC,j.available_at,j.created_at,j.id'
            row = await conn.fetchrow(
                'SELECT j.id,j.message_id,j.attempts FROM v2_analysis_jobs j JOIN v2_expert_messages m ON m.id=j.message_id '
                'WHERE j.source_id=$3 AND j.expert_id=$4 AND j.attempts<$1 AND ' + lane + ' AND ' + condition + ' AND ' + ELIGIBLE_MESSAGE +
                ' ORDER BY ' + order + ' LIMIT 1 FOR UPDATE OF j SKIP LOCKED',
                MAX_ATTEMPTS, REALTIME_WINDOW_SECONDS, pair['source_id'], pair['expert_id'])
            if row:
                return row, pair, turn, probe + 1
    return None


async def claim_job(pool):
    try:
        async with asyncio.timeout(CLAIM_TIMEOUT_SECONDS):
            job = await _claim_job(pool)
            if job:
                job['_claimed_monotonic'] = time.monotonic()
            return job
    except (TimeoutError, asyncpg.LockNotAvailableError, asyncpg.QueryCanceledError) as exc:
        # Transient scheduling contention must not cancel this process's
        # unrelated, still-valid model calls in run()'s shutdown path.
        log.warning('review scheduling deferred category=%s', type(exc).__name__)
        return None


async def _claim_job(pool):
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not await _scheduler_lock(conn):
                return None
            await conn.execute(
                "WITH exhausted AS(SELECT id FROM v2_analysis_jobs WHERE state='running' "
                "AND (lease_until IS NULL OR lease_until<=statement_timestamp()) AND attempts >= $1 "
                "ORDER BY lease_until NULLS FIRST,id LIMIT $2 FOR UPDATE SKIP LOCKED) "
                "UPDATE v2_analysis_jobs j SET state='failed',lease_until=NULL,last_error='lease_exhausted',updated_at=now() "
                "FROM exhausted WHERE j.id=exhausted.id", MAX_ATTEMPTS, EXHAUSTED_BATCH_SIZE,
            )
            # Only metadata is enumerated. Job probes are indexed per source /
            # expert pair; no global queue sort/materialization is performed.
            await conn.execute('INSERT INTO v2_review_source_schedule(source_id) SELECT id FROM v2_expert_sources ON CONFLICT DO NOTHING')
            await conn.execute('INSERT INTO v2_review_expert_schedule(expert_id) SELECT id FROM v2_experts ON CONFLICT DO NOTHING')
            realtime_claims = await conn.fetchval('SELECT realtime_claims_since_replay FROM v2_review_lane_schedule WHERE singleton FOR UPDATE')
            historical_active = await conn.fetchval(
                "SELECT count(*) FROM v2_analysis_jobs WHERE state='running' AND review_mode='historical_replay' "
                "AND lease_until>statement_timestamp()")
            replay_allowed = HISTORICAL_CONCURRENCY > historical_active
            modes = ['real_time']
            if replay_allowed:
                modes = (['historical_replay','real_time'] if realtime_claims >= REALTIME_CLAIMS_PER_REPLAY
                         else ['real_time','historical_replay'])
            claimed = None
            for mode in modes:
                claimed = await _claim_from_lane(conn, mode)
                if claimed:
                    break
            if not claimed:
                return None
            row, pair, turn, probes = claimed
            attempt = uuid.uuid4()
            lease_until = await conn.fetchval(
                "UPDATE v2_analysis_jobs SET state='running',attempt_id=$2,attempts=attempts+1,review_mode=$4,"
                "lease_until=clock_timestamp()+($3::int*interval '1 second'),heartbeat_at=clock_timestamp(),last_error=NULL,updated_at=now() WHERE id=$1 RETURNING lease_until",
                row["id"], attempt, LEASE_SECONDS, mode,
            )
            await conn.execute('UPDATE v2_review_lane_schedule SET realtime_claims_since_replay=$1,updated_at=now() WHERE singleton',
                               0 if mode == 'historical_replay' else min(realtime_claims + 1, REALTIME_CLAIMS_PER_REPLAY))
            await conn.execute("DELETE FROM v2_match_searches WHERE job_id=$1", row["id"])
            message = await conn.fetchrow(
                "SELECT m.id,m.raw_text,m.media_objects,m.posted_at,m.revision,m.reply_context,m.created_at,"
                "m.created_at AS event_persisted_at,"
                "GREATEST(0,round((extract(epoch FROM(clock_timestamp()-m.created_at))*1000)::numeric,3))::double precision AS event_to_claim_ms,"
                "e.display_nickname,"
                "s.display_name AS source_name,"
                "COALESCE((SELECT r.raw_text FROM v2_expert_messages r WHERE r.source_id=m.source_id "
                "AND r.platform_message_id=m.reply_to_platform_message_id ORDER BY r.revision DESC LIMIT 1),m.reply_context->>'raw_text') AS reply_text "
                "FROM v2_expert_messages m JOIN v2_experts e ON e.id=m.expert_id "
                "JOIN v2_expert_sources s ON s.id=m.source_id WHERE m.id=$1", row["message_id"],
            )
            event_to_claim_ms = message['event_to_claim_ms']
            await trace(conn, row["id"], "attempt_claimed", {"attempt_id": str(attempt), "attempt": row["attempts"] + 1,
                'source_id': str(pair['source_id']), 'expert_id': str(pair['expert_id']), 'concurrency_limit': pair['concurrency_limit'],
                'active_expert_leases_before': pair['active_leases'], 'scheduler_turn': turn, 'pair_probes': probes,
                'review_mode': mode, 'realtime_window_seconds': REALTIME_WINDOW_SECONDS,
                'message_age_seconds': max(0, round((datetime.now(timezone.utc)-message['posted_at']).total_seconds(),3)),
                'queue_wait_ms': max(0, round((datetime.now(timezone.utc) - message['created_at']).total_seconds() * 1000, 3)),
                'event_persisted_at': message['event_persisted_at'].isoformat()
                    if message['event_persisted_at'] else None,
                'event_to_claim_ms': event_to_claim_ms})
            return {"id": row["id"], "attempt_id": attempt, "attempts": row["attempts"] + 1, "message": dict(message),
                    'source_id': pair['source_id'], 'expert_id': pair['expert_id'], 'lease_until': lease_until, 'review_mode': mode}


async def _add_trace(pool, job: dict, kind: str, payload: dict) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await active_job(conn, job["id"], job["attempt_id"])
            await trace(conn, job["id"], kind, {"attempt_id": str(job["attempt_id"]), **payload})


async def _renew_lease(pool, job, *, budget=5.0):
    try:
        async with asyncio.timeout(budget):
            return await _renew_lease_once(pool, job)
    except TimeoutError:
        return None  # a bounded pool / database delay does not prove lease loss


async def _renew_lease_once(pool, job):
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await _scheduler_lock(conn):
                    return None  # contention is not lost ownership
                try:
                    await active_job(conn, job['id'], job['attempt_id'])
                except HTTPException as exc:
                    if exc.status_code == 409:
                        return False
                    raise
                return await conn.fetchval(
                    "UPDATE v2_analysis_jobs SET lease_until=clock_timestamp()+($3::int*interval '1 second'),"
                    "heartbeat_at=clock_timestamp(),updated_at=now() WHERE id=$1 AND attempt_id=$2 AND state='running' "
                    "AND lease_until>statement_timestamp() RETURNING lease_until", job['id'], job['attempt_id'], LEASE_SECONDS) or False
    except (asyncpg.LockNotAvailableError, asyncpg.QueryCanceledError):
        return None


async def keep_lease(pool, job: dict) -> None:
    until = job.get('lease_until')
    remaining = (until - datetime.now(timezone.utc)).total_seconds() if until else LEASE_SECONDS
    deadline = time.monotonic() + max(0, remaining)
    delay = HEARTBEAT_SECONDS
    while True:
        await asyncio.sleep(min(delay, max(0, deadline - time.monotonic())))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LeaseLost('review heartbeat could not renew before expiry')
        result = await _renew_lease(pool, job, budget=min(5.0, remaining))
        if result is False:
            raise LeaseLost('review lease or source eligibility no longer belongs to this worker')
        if result is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LeaseLost('review heartbeat could not renew before expiry')
            delay = min(.2, remaining)
        else:
            deadline = time.monotonic() + max(0, (result - datetime.now(timezone.utc)).total_seconds())
            delay = HEARTBEAT_SECONDS


async def process_job(pool, settings, job: dict, *, model_client: httpx.AsyncClient | None = None) -> None:
    review_task = lease_task = None
    started = time.monotonic()
    try:
        if model_client is None:
            settings = await resolve_review_settings(settings)
        else:
            settings = await resolve_review_settings(settings, client=model_client)
        await _add_trace(pool, job, "hermes_request", {
            "message_id": str(job["message"]["id"]), "raw_text": job["message"]["raw_text"],
            "reply_text": job["message"].get("reply_text"), "media_objects": job["message"]["media_objects"],
            "model": settings.hermes_model, "worker_attempt": job["attempts"],
            "review_mode": job.get('review_mode','real_time'),
            "configuration_version": getattr(settings, "configuration_version", None),
        })
        review_message_content = job["message"] | {'review_mode': job.get('review_mode','real_time')}
        if model_client is not None:
            review_call = review_message(settings, review_message_content, job["id"], job["attempt_id"],
                                         client=model_client)
        else:
            review_call = review_message(settings, review_message_content, job["id"], job["attempt_id"])
        review_task = asyncio.create_task(asyncio.wait_for(review_call, timeout=REVIEW_TIMEOUT))
        lease_task = asyncio.create_task(keep_lease(pool, job))
        done, _ = await asyncio.wait({review_task, lease_task}, return_when=asyncio.FIRST_COMPLETED)
        if lease_task in done:
            await lease_task
            raise LeaseLost("heartbeat ended")
        _, summary = await review_task
        claim_elapsed_ms = max(0, (time.monotonic() - job.get('_claimed_monotonic', started)) * 1000)
        event_to_claim_ms = job['message'].get('event_to_claim_ms')
        await _add_trace(pool, job, "hermes_result", {**summary,
            "model_elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "claim_to_result_ms": round(claim_elapsed_ms, 3),
            "event_to_result_ms": round(event_to_claim_ms + claim_elapsed_ms, 3)
            if event_to_claim_ms is not None else None})
        async with pool.acquire() as conn:
            async with conn.transaction():
                current = await active_job(conn, job["id"], job["attempt_id"])
                count = await record_summary(conn, current, job["attempt_id"], job.get('review_mode', 'real_time'))
                final_state = "succeeded" if count else "unresolved"
                conclusion_elapsed_ms = max(0, (time.monotonic() - job.get('_claimed_monotonic', started)) * 1000)
                concluded = await conn.fetchrow(
                    "WITH completed_job AS ("
                    " UPDATE v2_analysis_jobs j SET state=$3,lease_until=NULL,last_error=NULL,updated_at=now()"
                    " FROM v2_expert_messages m WHERE j.id=$1 AND j.attempt_id=$2 AND j.state='running'"
                    " AND m.id=j.message_id RETURNING j.id,"
                    " GREATEST(0,round((extract(epoch FROM (clock_timestamp()-m.created_at))*1000)::numeric,3)) AS event_elapsed"
                    "), completed_trace AS ("
                    " UPDATE v2_analysis_traces t SET payload=t.payload || jsonb_build_object("
                    "  'outcome',$3::text,'signal_count',$4::int,'claim_to_conclusion_ms',$5::numeric,"
                    "  'event_to_conclusion_ms',c.event_elapsed)"
                    " FROM completed_job c WHERE t.job_id=c.id AND t.trace_kind='system_review_result'"
                    " AND t.payload->>'attempt_id'=$2::text RETURNING t.id"
                    ") SELECT (SELECT count(*) FROM completed_job) AS job_count,"
                    "(SELECT count(*) FROM completed_trace) AS trace_count",
                    job["id"], job["attempt_id"], final_state, count, round(conclusion_elapsed_ms, 3),
                )
                if concluded["job_count"] != 1 or concluded["trace_count"] != 1:
                    raise RuntimeError("review conclusion state or receipt could not be committed")
    except asyncio.CancelledError:
        # 退出后保留租约，其他工作器待到期接管，不覆盖已接受推荐。
        raise
    except Exception as exc:
        category = type(exc).__name__
        failure, retryable = failure_category(exc)
        log.warning("review failed review_id=%s attempt_id=%s category=%s", job["id"], job["attempt_id"], category)
        exhausted = job["attempts"] >= MAX_ATTEMPTS or not retryable
        delay = 0 if exhausted else min(300, 5 * 2 ** (job["attempts"] - 1)) + random.randint(0, 3)
        try:
            await _add_trace(pool, job, "review_failure", {
                "failure_category": failure, "retryable": retryable, "exception_type": category,
                "attempt": job["attempts"], "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            })
        except Exception:
            # A lost lease or unavailable database must not mask the original
            # bounded failure classification or prevent cleanup.
            pass
        await pool.execute(
            "UPDATE v2_analysis_jobs SET state=$3,available_at=now()+($4::int*interval '1 second'),"
            "lease_until=NULL,last_error=$5,updated_at=now() WHERE id=$1 AND attempt_id=$2 AND state='running'",
            job["id"], job["attempt_id"], "failed" if exhausted else "retry", delay, category,
        )
    finally:
        pending = [task for task in (review_task, lease_task) if task is not None]
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            if model_client is None:
                await delete_session(settings, job["id"], job["attempt_id"])
            else:
                await delete_session(settings, job["id"], job["attempt_id"], client=model_client)
        except Exception as exc:
            log.warning("session cleanup failed review_id=%s category=%s", job["id"], type(exc).__name__)


async def listen_for_jobs(database_url: str, wake: asyncio.Event, stop: asyncio.Event) -> None:
    """Wake on committed queue changes; periodic checks remain the fallback."""
    delay = 1
    while not stop.is_set():
        connection = None
        try:
            connection = await asyncpg.connect(
                database_url, timeout=3, command_timeout=5,
                server_settings={'application_name': 'ai-football-review-notify', 'statement_timeout': '5000'},
            )
            await connection.add_listener(JOB_READY_CHANNEL, lambda *_: wake.set())
            log.info('review job notification listener connected')
            delay = 1
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except TimeoutError:
                    if connection.is_closed():
                        raise ConnectionError('review notification connection closed')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('review notification listener deferred category=%s', type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                delay = min(15, delay * 2)
        finally:
            if connection is not None and not connection.is_closed():
                await connection.close()


async def run() -> None:
    settings = load_settings(role="worker")
    pool = await database.open_pool(settings)
    active: set[asyncio.Task] = set()
    wake, stop = asyncio.Event(), asyncio.Event()
    model_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=3.0), trust_env=False,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=8, keepalive_expiry=30.0))
    listener = asyncio.create_task(listen_for_jobs(settings.database_url, wake, stop), name='review-job-notifications')
    try:
        while True:
            wake.clear()
            while len(active) < CONCURRENCY:
                job = await claim_job(pool)
                if not job:
                    break
                task = asyncio.create_task(process_job(pool, settings, job, model_client=model_client))
                active.add(task)
                task.add_done_callback(active.discard)
            if active:
                await asyncio.wait(active, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
            else:
                try:
                    await asyncio.wait_for(wake.wait(), timeout=1.0)
                except TimeoutError:
                    pass
    finally:
        stop.set()
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        await model_client.aclose()
        await database.close_pool()


if __name__ == "__main__":
    asyncio.run(run())
