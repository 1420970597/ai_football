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
from .hermes_client import (HermesProviderUnavailable, ReplyMediaPending, delete_session,
                            resolve_and_prepare, resolve_review_settings, review_message)
from .review_state import active_job, append_traces, trace
from .review_summary import SUMMARY_ITEM_LIMIT, record_summary
from .review_summary_evidence import read_summary_evidence
from .review_recovery import market_recovery
from .review_phase_claims import current_phase_claims, named_phase_claims
from .review_line_claims import split_line_claim_conflicts
from .review_quote_claims import missing_handicap_claims
from .review_accepted_context import read_accepted_context
from .review_completion import (completion_issues, continuation_prompt, mentioned_fixtures, named_identity_claims, explicit_text_pick,
                                explicit_visual_instruction,
                                tool_recovery_pending, tool_unavailability_claim, tool_unavailability_issues)
from .review_policy import FRESH_PRIORITY_WINDOW_SECONDS, REALTIME_WINDOW_SECONDS
from .review_scheduler_setup import claim_pair_and_job_query, prepare_claim_schedule
from fastapi import HTTPException
import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("platform.review-worker")
CONCURRENCY = max(1, min(8, int(os.environ.get("REVIEW_WORKER_CONCURRENCY", "2"))))
EXPERT_CONCURRENCY_FLOOR = max(1, min(CONCURRENCY, int(os.environ.get("REVIEW_EXPERT_CONCURRENCY_FLOOR", "1"))))
MAX_ATTEMPTS = 5
PERSISTENT_RETRY_FAILURES = frozenset({'provider_transient', 'timeout', 'network', 'reply_media_pending'})
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
FRESH_AGING_THRESHOLD_SECONDS = 60
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


class ReviewEvidenceUnavailable(TimeoutError):
    """A returned model result could not be checked against current tool evidence."""


def failure_category(exc: BaseException) -> tuple[str, bool]:
    """Return a bounded, non-sensitive failure class and retry policy."""
    if isinstance(exc, ReplyMediaPending):
        return 'reply_media_pending', True
    if isinstance(exc, LeaseLost):
        return "lease_lost", True
    if isinstance(exc, HermesProviderUnavailable):
        return "provider_transient", True
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


def retry_delay_seconds(failure: str, attempts: int) -> int:
    attempt = max(1, attempts)
    if failure in PERSISTENT_RETRY_FAILURES:
        delay = min(900, 60 * 2 ** min(attempt - 1, 4))
        return delay + random.randint(0, min(60, max(1, delay // 10)))
    initial = 60 if failure == "provider_transient" else 5
    return min(300, initial * 2 ** (attempt - 1)) + random.randint(0, 3)


async def _scheduler_lock(conn):
    row = await conn.fetchrow(
        "SELECT set_config('lock_timeout','2000',true),set_config('statement_timeout','3000',true),"
        'pg_try_advisory_xact_lock(hashtext($1)) AS locked', SCHEDULER_LOCK)
    return row['locked']


def _turn_advance_ctes(source_parameter='$1', expert_parameter='$2'):
    return (
        'next_turn AS MATERIALIZED ('
        ' SELECT GREATEST(COALESCE((SELECT max(last_turn) FROM v2_review_source_schedule),0),'
        ' COALESCE((SELECT max(last_turn) FROM v2_review_expert_schedule),0))+1 AS turn'
        '), advanced_source AS ('
        ' UPDATE v2_review_source_schedule s SET last_turn=n.turn FROM next_turn n'
        f' WHERE s.source_id={source_parameter} RETURNING s.source_id'
        '), advanced_expert AS ('
        ' UPDATE v2_review_expert_schedule e SET last_turn=n.turn FROM next_turn n'
        f' WHERE e.expert_id={expert_parameter} AND EXISTS(SELECT 1 FROM advanced_source)'
        ' RETURNING e.last_turn)'
    )


async def _advance_turn(conn, source_id, expert_id):
    turn = await conn.fetchval(
        'WITH ' + _turn_advance_ctes() + ' SELECT last_turn FROM advanced_expert',
        source_id, expert_id)
    if turn is None:
        raise RuntimeError('review scheduling pair disappeared while advancing turn')
    return turn


def _lane_condition(mode, realtime_parameter='$2', fresh_parameter='$3'):
    realtime_bound = f"statement_timestamp()-({realtime_parameter}::int*interval '1 second')"
    fresh_bound = f"statement_timestamp()-({fresh_parameter}::int*interval '1 second')"
    valid_window_order = f'{fresh_parameter}::int <= {realtime_parameter}::int'
    if mode == 'fresh_real_time':
        return ('j.message_posted_at>=' + fresh_bound + ' AND j.message_posted_at<=statement_timestamp() AND '
                + valid_window_order)
    if mode == 'real_time':
        return 'j.message_posted_at>=' + realtime_bound + ' AND j.message_posted_at<' + fresh_bound
    return 'j.message_posted_at<' + realtime_bound + ' AND ' + valid_window_order


async def _claim_from_lane(conn, mode):
    lane = _lane_condition(mode)
    # Keep the lane's original posting-age eligibility. Within that lane,
    # sustained newer arrivals must not repeatedly overtake an already waiting
    # message. Running lease recovery still sorts before either queued group.
    waiting_order = (
        "CASE WHEN j.state<>'running' AND j.created_at<=statement_timestamp()"
        f"-({FRESH_AGING_THRESHOLD_SECONDS}*interval '1 second') THEN j.created_at END ASC NULLS LAST,"
        if mode in {'fresh_real_time', 'real_time'} else ''
    )
    query = claim_pair_and_job_query(lane, ELIGIBLE_MESSAGE, waiting_order, _turn_advance_ctes(
        '(SELECT source_id FROM selected_pair)', '(SELECT expert_id FROM selected_pair)'))
    for probe in range(MAX_PAIR_PROBES):
        selected = await conn.fetchrow(
            query,
            MAX_ATTEMPTS, REALTIME_WINDOW_SECONDS, FRESH_PRIORITY_WINDOW_SECONDS, EXPERT_CONCURRENCY_FLOOR,
            sorted(PERSISTENT_RETRY_FAILURES))
        if not selected:
            return None
        turn = selected['scheduler_turn']
        if turn is None:
            raise RuntimeError('review scheduling pair disappeared while advancing turn')
        if selected['id'] is not None:
            row = {key: selected[key] for key in ('id', 'message_id', 'attempts')}
            pair = {key: selected[key] for key in ('source_id', 'expert_id', 'concurrency_limit', 'active_leases')}
            return row, pair, turn, probe + 1
    return None


async def _activate_claim(conn, job_id, attempt, mode, realtime_claims):
    """Activate an already locked job and return its message in one round trip."""
    row = await conn.fetchrow(
        "WITH claimed AS (UPDATE v2_analysis_jobs SET state='running',attempt_id=$2,attempts=attempts+1,review_mode=$4,"
        "lease_until=clock_timestamp()+($3::int*interval '1 second'),heartbeat_at=clock_timestamp(),last_error=NULL,updated_at=now() "
        "WHERE id=$1 RETURNING id,message_id,lease_until), advanced_lane AS ("
        'UPDATE v2_review_lane_schedule SET realtime_claims_since_replay=$5,updated_at=now() '
        'WHERE singleton AND EXISTS(SELECT 1 FROM claimed) RETURNING singleton), cleared_searches AS ('
        'DELETE FROM v2_match_searches WHERE job_id IN(SELECT id FROM claimed) RETURNING job_id) '
        'SELECT m.id,m.raw_text,m.media_objects,m.posted_at,m.revision,m.reply_context,m.created_at,'
        'm.created_at AS event_persisted_at,'
        "GREATEST(0,round((extract(epoch FROM(clock_timestamp()-m.created_at))*1000)::numeric,3))::double precision AS event_to_claim_ms,"
        'e.display_nickname,s.display_name AS source_name,c.lease_until AS claimed_lease_until,'
        "pg_advisory_xact_lock(hashtextextended('v2-review-trace:' || ($1::uuid)::text,0)) AS trace_lock,"
        "COALESCE(CASE WHEN r.deleted_at IS NULL THEN r.raw_text END,"
        "m.reply_context->>'text',m.reply_context->>'raw_text') AS reply_text,"
        "CASE WHEN r.id IS NULL THEN NULL ELSE jsonb_build_object('id',r.id,'revision',r.revision,"
        "'posted_at',r.posted_at,'expert_id',r.expert_id,'media_state',r.media_state,"
        "'deleted',r.deleted_at IS NOT NULL,'media_objects',CASE WHEN r.deleted_at IS NULL "
        "THEN r.media_objects ELSE '[]'::jsonb END) END AS reply_archive "
        'FROM claimed c JOIN v2_expert_messages m ON m.id=c.message_id '
        'JOIN v2_experts e ON e.id=m.expert_id JOIN v2_expert_sources s ON s.id=m.source_id '
        'LEFT JOIN LATERAL(SELECT r.* FROM v2_expert_messages r WHERE r.source_id=m.source_id AND ('
        'r.platform_message_id=m.reply_to_platform_message_id OR r.id IN('
        'SELECT ev.message_id FROM v2_telegram_events ev JOIN v2_telegram_event_members em ON em.event_id=ev.id '
        "WHERE ev.source_id=m.source_id AND em.platform_message_id=CASE WHEN m.reply_to_platform_message_id ~ '^[0-9]{1,18}$' "
        'THEN m.reply_to_platform_message_id::bigint ELSE NULL END)) '
        'ORDER BY r.revision DESC,r.created_at DESC,r.id DESC LIMIT 1) r ON true',
        job_id, attempt, LEASE_SECONDS, mode,
        0 if mode == 'historical_replay' else min(realtime_claims + 1, REALTIME_CLAIMS_PER_REPLAY))
    if row is None:
        raise RuntimeError('claimed review job or message disappeared during activation')
    message = dict(row)
    message.pop('trace_lock')
    return message.pop('claimed_lease_until'), message


async def claim_job(pool):
    started = time.monotonic()
    try:
        async with asyncio.timeout(CLAIM_TIMEOUT_SECONDS):
            job = await _claim_job(pool)
            if job:
                job['_claimed_monotonic'] = time.monotonic()
                job['_scheduler_claim_elapsed_ms'] = round((job['_claimed_monotonic'] - started) * 1000, 3)
            return job
    except (TimeoutError, asyncpg.LockNotAvailableError, asyncpg.QueryCanceledError) as exc:
        # Transient scheduling contention must not cancel this process's
        # unrelated, still-valid model calls in run()'s shutdown path.
        log.warning('review scheduling deferred category=%s', type(exc).__name__)
        return None


async def _claim_job(pool):
    async with pool.acquire() as conn:
        async with conn.transaction():
            realtime_claims = await prepare_claim_schedule(
                conn, MAX_ATTEMPTS, EXHAUSTED_BATCH_SIZE, PERSISTENT_RETRY_FAILURES,
                scheduler_lock=SCHEDULER_LOCK)
            if realtime_claims is None:
                return None
            replay_allowed = False
            if HISTORICAL_CONCURRENCY > 0:
                historical_active = await conn.fetchval(
                    "SELECT count(*) FROM v2_analysis_jobs WHERE state='running' AND review_mode='historical_replay' "
                    "AND lease_until>statement_timestamp()")
                replay_allowed = HISTORICAL_CONCURRENCY > historical_active
            realtime_modes = ['fresh_real_time', 'real_time']
            if replay_allowed:
                modes = (['historical_replay', *realtime_modes] if realtime_claims >= REALTIME_CLAIMS_PER_REPLAY
                         else [*realtime_modes, 'historical_replay'])
            else:
                modes = realtime_modes
            claimed = None
            scheduler_lane = None
            for lane in modes:
                claimed = await _claim_from_lane(conn, lane)
                if claimed:
                    scheduler_lane = lane
                    break
            if not claimed:
                return None
            row, pair, turn, probes = claimed
            mode = 'historical_replay' if scheduler_lane == 'historical_replay' else 'real_time'
            attempt = uuid.uuid4()
            lease_until, message = await _activate_claim(conn, row['id'], attempt, mode, realtime_claims)
            event_to_claim_ms = message['event_to_claim_ms']
            # Activation already holds this task's trace lock. Allocate the
            # sequence in a later statement, after any prior writer committed.
            await append_traces(conn, row["id"], [("attempt_claimed", {"attempt_id": str(attempt), "attempt": row["attempts"] + 1,
                'source_id': str(pair['source_id']), 'expert_id': str(pair['expert_id']), 'concurrency_limit': pair['concurrency_limit'],
                'active_expert_leases_before': pair['active_leases'], 'scheduler_turn': turn, 'pair_probes': probes,
                'review_mode': mode, 'scheduler_lane': scheduler_lane,
                'fresh_priority_window_seconds': FRESH_PRIORITY_WINDOW_SECONDS,
                'fresh_aging_threshold_seconds': FRESH_AGING_THRESHOLD_SECONDS,
                'realtime_window_seconds': REALTIME_WINDOW_SECONDS,
                'message_age_seconds': max(0, round((datetime.now(timezone.utc)-message['posted_at']).total_seconds(),3)),
                'queue_wait_ms': max(0, round((datetime.now(timezone.utc) - message['created_at']).total_seconds() * 1000, 3)),
                'event_persisted_at': message['event_persisted_at'].isoformat()
                    if message['event_persisted_at'] else None,
                'event_to_claim_ms': event_to_claim_ms})], trace_lock_held=True)
            return {"id": row["id"], "attempt_id": attempt, "attempts": row["attempts"] + 1, "message": dict(message),
                    'source_id': pair['source_id'], 'expert_id': pair['expert_id'], 'lease_until': lease_until,
                    'review_mode': mode, 'scheduler_lane': scheduler_lane}


async def _add_trace(pool, job: dict, kind: str, payload: dict) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await active_job(conn, job["id"], job["attempt_id"], lock_trace_sequence=True)
            await append_traces(conn, job["id"], [
                (kind, {"attempt_id": str(job["attempt_id"]), **payload}),
            ], trace_lock_held=True)


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


async def _complete_review_once(pool, job: dict, started: float, model_result: dict | None = None) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Reuse the trace lock acquired in this eligibility query. The
            # receipt insert remains a later statement so a concurrent trace
            # committed before lock acquisition is visible to its sequence read.
            current = await active_job(conn, job["id"], job["attempt_id"], lock_trace_sequence=True)
            if model_result is not None:
                # The model evidence and its receipt share eligibility, sequence
                # ownership and COMMIT. A rollback retries the saved response,
                # never the provider call or a separately committed duplicate.
                await append_traces(conn, job['id'], [('hermes_result',
                                    {**model_result, 'attempt_id': str(job['attempt_id'])})],
                                    trace_lock_held=True)
            # Read after the trace lock: a tool receipt committed while we
            # waited must participate in both the summary and recovery check.
            evidence = await read_summary_evidence(
                conn, current['id'], job['attempt_id'], current['message_id'], SUMMARY_ITEM_LIMIT)
            count = await record_summary(conn, current, job["attempt_id"], job.get('review_mode', 'real_time'),
                                         trace_lock_held=True, evidence=evidence)
            recovery = (await market_recovery(conn, current, job['attempt_id'], job['attempts'])
                        if evidence.has_market_tool_receipts else None)
            final_state = 'retry' if recovery else "succeeded" if count else "unresolved"
            if recovery:
                await append_traces(conn, job['id'], [('market_recovery_scheduled',
                                    {'attempt_id': str(job['attempt_id']), **recovery})], trace_lock_held=True)
            conclusion_elapsed_ms = max(0, (time.monotonic() - job.get('_claimed_monotonic', started)) * 1000)
            concluded = await conn.fetchrow(
                "WITH completed_job AS ("
                " UPDATE v2_analysis_jobs j SET state=$3,lease_until=NULL,last_error=$6,"
                " available_at=CASE WHEN $3='retry' THEN now()+($7::int*interval '1 second') ELSE j.available_at END,updated_at=now()"
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
                recovery['reason'] if recovery else None, recovery['delay_seconds'] if recovery else 0,
            )
            if concluded["job_count"] != 1 or concluded["trace_count"] != 1:
                raise RuntimeError("review conclusion state or receipt could not be committed")


async def _conclusion_committed(pool, job: dict) -> bool:
    # A disconnected COMMIT acknowledgement is not evidence of rollback.
    # The receipt and terminal state must belong to this exact attempt.
    return bool(await pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM v2_analysis_jobs j "
        "JOIN v2_analysis_traces t ON t.job_id=j.id "
        "WHERE j.id=$1 AND j.attempt_id=$2 AND j.state IN ('succeeded','unresolved','retry') "
        "AND t.trace_kind='system_review_result' AND t.payload->>'attempt_id'=$2::text "
        "AND t.payload->>'outcome'=j.state)", job['id'], job['attempt_id']))


async def _complete_review(pool, job: dict, started: float, model_result: dict | None = None) -> None:
    transient = (TimeoutError, asyncpg.LockNotAvailableError, asyncpg.QueryCanceledError,
                 asyncpg.DeadlockDetectedError, asyncpg.SerializationError,
                 asyncpg.PostgresConnectionError)
    async with asyncio.timeout(15):
        for attempt in range(2):
            try:
                if model_result is None:
                    await _complete_review_once(pool, job, started)
                else:
                    await _complete_review_once(pool, job, started, model_result)
                return
            except transient:
                if await _conclusion_committed(pool, job):
                    return
                if attempt == 1:
                    raise
                await asyncio.sleep(.1)


async def _read_completion_issues(pool, job, mentioned, *, tool_claim=False, require_tool_recovery=False,
                                  named_content='', require_intent_review=False, final_content='',
                                  require_visual_instruction_review=False):
    transient = (TimeoutError, asyncpg.LockNotAvailableError, asyncpg.QueryCanceledError,
                 asyncpg.DeadlockDetectedError, asyncpg.SerializationError, asyncpg.PostgresConnectionError)
    for retry in range(2):
        try:
            async with pool.acquire() as conn, conn.transaction():
                await active_job(conn, job['id'], job['attempt_id'])
                args = ((named_content, True) if require_intent_review
                        else (named_content,) if named_content else ())
                phase_options = {'final_content': final_content} if final_content else {}
                if require_visual_instruction_review:
                    phase_options['require_visual_instruction_review'] = True
                issues = await completion_issues(conn, job['id'], job['attempt_id'], mentioned, *args, **phase_options)
                if tool_claim or (require_tool_recovery
                        and await tool_recovery_pending(conn, job['id'], job['attempt_id'])):
                    issues = {**tool_unavailability_issues(), **issues}
                return issues, retry
        except transient as exc:
            if retry:
                raise ReviewEvidenceUnavailable('current review evidence temporarily unavailable') from exc
            await asyncio.sleep(.1)


async def _review_until_verified(pool, settings, job: dict, *, model_client=None, prepared_input=None):
    """All turns share the caller's overall deadline, heartbeat and frozen config."""
    mentioned = set()
    named_contents = []
    issues = {}
    totals = {}
    usage_totals = {}
    check_retries = 0
    message = job['message'] | {'review_mode': job.get('review_mode', 'real_time')}
    accepted_context = None
    tool_recovery_required = False
    if job.get('attempts', 1) > 1 and message['review_mode'] == 'real_time':
        if prepared_input is not None:
            raise ValueError('recovery review cannot reuse an initial prepared input')
        try:
            async with pool.acquire() as conn, conn.transaction():
                accepted_context = await read_accepted_context(conn, job['id'], job['attempt_id'])
        except (TimeoutError, asyncpg.PostgresError) as exc:
            raise ReviewEvidenceUnavailable('recovery evidence temporarily unavailable') from exc
        if accepted_context['accepted']:
            await _add_trace(pool, job, 'recovery_accepted_context', accepted_context)
        else:
            # A provider retry before any acceptance needs the original input,
            # without an empty hint, extra trace or new model-call argument.
            accepted_context = None
    for turn in range(3):
        kwargs = {'client': model_client} if model_client is not None else {}
        if prepared_input is not None and not turn:
            kwargs['prepared_input'] = prepared_input
        if accepted_context is not None and not turn:
            kwargs['accepted_context'] = accepted_context
        if turn:
            kwargs.update(continuation=continuation_prompt(issues), continuation_round=turn)
        content, summary = await review_message(settings, message, job['id'], job['attempt_id'], **kwargs)
        for key in ('image_count', 'image_bytes', 'input_prepare_elapsed_ms', 'provider_elapsed_ms'):
            if isinstance(summary.get(key), (int, float)):
                totals[key] = totals.get(key, 0) + summary[key]
        for key, value in (summary.get('usage') or {}).items():
            if isinstance(value, (int, float)):
                usage_totals[key] = usage_totals.get(key, 0) + value
        mentioned.update(mentioned_fixtures(content))
        named_contents.append(content)
        tool_claim = message['review_mode'] == 'real_time' and tool_unavailability_claim(content)
        recovery_options = ({'tool_claim': tool_claim, 'require_tool_recovery': tool_recovery_required}
                            if tool_claim or tool_recovery_required else {})
        named_content = named_identity_claims(named_contents)
        if named_content:
            recovery_options['named_content'] = named_content
        if (current_phase_claims(content) or named_phase_claims(content) or split_line_claim_conflicts(content)
                or missing_handicap_claims(content)):
            recovery_options['final_content'] = content
        if (message['review_mode'] == 'real_time' and explicit_visual_instruction(
                message.get('raw_text', ''), any(
                    isinstance(media, dict) and str(media.get('mime_type', '')).lower().startswith('image/')
                    for media in message.get('media_objects', [])))):
            recovery_options.update(require_visual_instruction_review=True, final_content=content)
        if (message['review_mode'] == 'real_time' and not accepted_context
                and explicit_text_pick(message.get('raw_text', ''))):
            recovery_options['require_intent_review'] = True
        issues, retries = await _read_completion_issues(pool, job, mentioned, **recovery_options)
        tool_recovery_required = bool(issues.get('tool_availability_unverified'))
        check_retries += retries
        summary = {**summary, 'completion_verified': not issues, 'completion_rounds': turn + 1,
                   'completion_issues': issues, 'completion_check_retries': check_retries}
        if turn == 2 and tool_recovery_required:
            await _add_trace(pool, job, 'hermes_partial_result', summary)
            raise HermesProviderUnavailable()
        if not issues or turn == 2:
            summary.update(totals)
            if usage_totals:
                summary['usage'] = usage_totals
            return content, summary
        await _add_trace(pool, job, 'hermes_partial_result', summary)
    raise RuntimeError('review continuation budget exhausted')


async def process_job(pool, settings, job: dict, *, model_client: httpx.AsyncClient | None = None) -> None:
    review_task = lease_task = None
    model_result = None
    started = time.monotonic()
    failure_stage = "configuration"
    try:
        prepared_input = None
        preflight_started = time.monotonic()
        reply = job['message'].get('reply_archive') or {}
        has_images = bool(job['message'].get('media_objects') or (
            reply.get('media_state') == 'ready' and not reply.get('deleted') and reply.get('media_objects')))
        if job.get('attempts', 1) == 1 and has_images and getattr(settings, 'review_config_url', None):
            settings, prepared_input = await resolve_and_prepare(
                settings, job['message'] | {'review_mode': job.get('review_mode', 'real_time')},
                job['id'], job['attempt_id'], client=model_client)
        elif model_client is None:
            settings = await resolve_review_settings(settings)
        else:
            settings = await resolve_review_settings(settings, client=model_client)
        preflight_elapsed_ms = round((time.monotonic() - preflight_started) * 1000, 3)
        failure_stage = "model_request_persistence"
        await _add_trace(pool, job, "hermes_request", {
            "message_id": str(job["message"]["id"]), "raw_text": job["message"]["raw_text"],
            "reply_text": job["message"].get("reply_text"), "media_objects": job["message"]["media_objects"],
            'reply_archive': job['message'].get('reply_archive'),
            "model": settings.hermes_model, "worker_attempt": job["attempts"],
            "review_mode": job.get('review_mode','real_time'),
            "configuration_version": getattr(settings, "configuration_version", None),
            'parallel_input_preparation': prepared_input is not None,
            'preflight_elapsed_ms': preflight_elapsed_ms,
        })
        review_options = {'prepared_input': prepared_input} if prepared_input is not None else {}
        review_call = _review_until_verified(pool, settings, job, model_client=model_client, **review_options)
        failure_stage = "model_review"
        review_task = asyncio.create_task(asyncio.wait_for(review_call, timeout=REVIEW_TIMEOUT))
        lease_task = asyncio.create_task(keep_lease(pool, job))
        done, _ = await asyncio.wait({review_task, lease_task}, return_when=asyncio.FIRST_COMPLETED)
        if lease_task in done:
            await lease_task
            raise LeaseLost("heartbeat ended")
        _, summary = await review_task
        # Both the heartbeat and the conclusion transaction lock this job row.
        # Stop renewal first so concurrent FOR SHARE -> UPDATE lock upgrades
        # cannot deadlock while the final receipt is being committed.
        lease_task.cancel()
        await asyncio.gather(lease_task, return_exceptions=True)
        lease_task = None
        claim_elapsed_ms = max(0, (time.monotonic() - job.get('_claimed_monotonic', started)) * 1000)
        event_to_claim_ms = job['message'].get('event_to_claim_ms')
        model_result = {**summary,
            "scheduler_claim_elapsed_ms": job.get('_scheduler_claim_elapsed_ms'),
            "model_elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "claim_to_result_ms": round(claim_elapsed_ms, 3),
            "event_to_result_ms": round(event_to_claim_ms + claim_elapsed_ms, 3)
            if event_to_claim_ms is not None else None}
        failure_stage = "conclusion_persistence"
        await _complete_review(pool, job, started, model_result)
    except asyncio.CancelledError:
        # 退出后保留租约，其他工作器待到期接管，不覆盖已接受推荐。
        raise
    except Exception as exc:
        if failure_stage == 'conclusion_persistence' and model_result is not None:
            try:
                # Keep the original model audit if both conclusion attempts
                # fail. active_job still rejects a cancelled/committed attempt;
                # a lost COMMIT acknowledgement cannot add a duplicate result.
                await _add_trace(pool, job, 'hermes_result', model_result)
            except Exception:
                pass
        if isinstance(exc, ReviewEvidenceUnavailable):
            failure_stage = 'completion_evidence'
        category = type(exc).__name__
        failure, retryable = failure_category(exc)
        log.warning("review failed review_id=%s attempt_id=%s category=%s", job["id"], job["attempt_id"], category)
        failure_trace_saved = False
        try:
            await _add_trace(pool, job, "review_failure", {
                "failure_category": failure, "retryable": retryable, "exception_type": category,
                "failure_stage": failure_stage, "attempt": job["attempts"], "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            })
            failure_trace_saved = True
        except Exception:
            # A lost lease or unavailable database must not mask the original
            # bounded failure classification or prevent cleanup.
            pass
        persistent_retry = failure_trace_saved and failure in PERSISTENT_RETRY_FAILURES
        exhausted = not retryable or (job["attempts"] >= MAX_ATTEMPTS and not persistent_retry)
        delay = 0 if exhausted else retry_delay_seconds(failure, job["attempts"])
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
