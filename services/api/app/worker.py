from __future__ import annotations

import asyncio
import logging
import os
import random
import uuid

from . import database
from .config import load_settings
from .hermes_client import delete_session, review_message
from .review_state import active_job, trace

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("platform.review-worker")
CONCURRENCY = max(1, min(8, int(os.environ.get("REVIEW_WORKER_CONCURRENCY", "2"))))
MAX_ATTEMPTS = 5
LEASE_SECONDS = 90
HEARTBEAT_SECONDS = 20
REVIEW_TIMEOUT = max(60, min(1800, int(os.environ.get("REVIEW_TIMEOUT_SECONDS", "600"))))


class LeaseLost(RuntimeError):
    pass


async def claim_job(pool):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE v2_analysis_jobs SET state='failed',lease_until=NULL,last_error='lease_exhausted',updated_at=now() "
                "WHERE state='running' AND lease_until<now() AND attempts >= $1", MAX_ATTEMPTS,
            )
            row = await conn.fetchrow(
                "SELECT j.id,j.message_id,j.attempts FROM v2_analysis_jobs j "
                "JOIN v2_expert_messages m ON m.id=j.message_id "
                "JOIN v2_experts e ON e.id=m.expert_id JOIN v2_expert_sources s ON s.id=m.source_id "
                "JOIN v2_source_experts se ON se.source_id=m.source_id AND se.expert_id=m.expert_id "
                "WHERE ((j.state IN ('queued','retry') AND j.available_at<=now()) "
                "OR (j.state='running' AND j.lease_until<now())) AND j.attempts<$1 "
                "AND m.deleted_at IS NULL AND m.media_state='ready' AND e.enabled AND s.enabled AND se.enabled "
                "AND NOT EXISTS(SELECT 1 FROM v2_expert_messages newer WHERE newer.source_id=m.source_id "
                "AND newer.platform_message_id=m.platform_message_id AND newer.revision>m.revision) "
                "ORDER BY j.available_at,j.created_at LIMIT 1 FOR UPDATE OF j SKIP LOCKED", MAX_ATTEMPTS,
            )
            if not row:
                return None
            attempt = uuid.uuid4()
            await conn.execute(
                "UPDATE v2_analysis_jobs SET state='running',attempt_id=$2,attempts=attempts+1,"
                "lease_until=now()+($3::int*interval '1 second'),heartbeat_at=now(),last_error=NULL,updated_at=now() WHERE id=$1",
                row["id"], attempt, LEASE_SECONDS,
            )
            await conn.execute("DELETE FROM v2_match_searches WHERE job_id=$1", row["id"])
            message = await conn.fetchrow(
                "SELECT m.id,m.raw_text,m.media_objects,m.posted_at,m.revision,m.reply_context,e.display_nickname,"
                "s.display_name AS source_name,"
                "COALESCE((SELECT r.raw_text FROM v2_expert_messages r WHERE r.source_id=m.source_id "
                "AND r.platform_message_id=m.reply_to_platform_message_id ORDER BY r.revision DESC LIMIT 1),m.reply_context->>'raw_text') AS reply_text "
                "FROM v2_expert_messages m JOIN v2_experts e ON e.id=m.expert_id "
                "JOIN v2_expert_sources s ON s.id=m.source_id WHERE m.id=$1", row["message_id"],
            )
            await trace(conn, row["id"], "attempt_claimed", {"attempt_id": str(attempt), "attempt": row["attempts"] + 1})
            return {"id": row["id"], "attempt_id": attempt, "attempts": row["attempts"] + 1, "message": dict(message)}


async def _add_trace(pool, job: dict, kind: str, payload: dict) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await active_job(conn, job["id"], job["attempt_id"])
            await trace(conn, job["id"], kind, {"attempt_id": str(job["attempt_id"]), **payload})


async def keep_lease(pool, job: dict) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        result = await pool.execute(
            "UPDATE v2_analysis_jobs SET lease_until=now()+($3::int*interval '1 second'),"
            "heartbeat_at=now(),updated_at=now() WHERE id=$1 AND attempt_id=$2 AND state='running' AND lease_until>now()",
            job["id"], job["attempt_id"], LEASE_SECONDS,
        )
        if result != "UPDATE 1":
            raise LeaseLost("review lease no longer belongs to this worker")


async def process_job(pool, settings, job: dict) -> None:
    review_task = lease_task = None
    try:
        await _add_trace(pool, job, "hermes_request", {
            "message_id": str(job["message"]["id"]), "raw_text": job["message"]["raw_text"],
            "reply_text": job["message"].get("reply_text"), "media_objects": job["message"]["media_objects"],
            "model": settings.hermes_model, "worker_attempt": job["attempts"],
        })
        review_task = asyncio.create_task(asyncio.wait_for(
            review_message(settings, job["message"], job["id"], job["attempt_id"]), timeout=REVIEW_TIMEOUT,
        ))
        lease_task = asyncio.create_task(keep_lease(pool, job))
        done, _ = await asyncio.wait({review_task, lease_task}, return_when=asyncio.FIRST_COMPLETED)
        if lease_task in done:
            await lease_task
            raise LeaseLost("heartbeat ended")
        _, summary = await review_task
        await _add_trace(pool, job, "hermes_result", summary)
        async with pool.acquire() as conn:
            async with conn.transaction():
                await active_job(conn, job["id"], job["attempt_id"])
                count = await conn.fetchval("SELECT count(*) FROM v2_expert_signals WHERE message_id=$1", job["message"]["id"])
                await conn.execute(
                    "UPDATE v2_analysis_jobs SET state=$3,lease_until=NULL,last_error=NULL,updated_at=now() "
                    "WHERE id=$1 AND attempt_id=$2 AND state='running'",
                    job["id"], job["attempt_id"], "succeeded" if count else "unresolved",
                )
    except asyncio.CancelledError:
        # 退出后保留租约，其他工作器待到期接管，不覆盖已接受推荐。
        raise
    except Exception as exc:
        category = type(exc).__name__
        log.warning("review failed review_id=%s attempt_id=%s category=%s", job["id"], job["attempt_id"], category)
        exhausted = job["attempts"] >= MAX_ATTEMPTS
        delay = 0 if exhausted else min(300, 5 * 2 ** (job["attempts"] - 1)) + random.randint(0, 3)
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
            await delete_session(settings, job["id"], job["attempt_id"])
        except Exception as exc:
            log.warning("session cleanup failed review_id=%s category=%s", job["id"], type(exc).__name__)


async def run() -> None:
    settings = load_settings(role="worker")
    pool = await database.open_pool(settings)
    active: set[asyncio.Task] = set()
    try:
        while True:
            while len(active) < CONCURRENCY:
                job = await claim_job(pool)
                if not job:
                    break
                task = asyncio.create_task(process_job(pool, settings, job))
                active.add(task)
                task.add_done_callback(active.discard)
            if active:
                await asyncio.wait(active, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
            else:
                await asyncio.sleep(1.0)
    finally:
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        await database.close_pool()


if __name__ == "__main__":
    asyncio.run(run())
