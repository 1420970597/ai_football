from __future__ import annotations

from datetime import datetime, timezone
import hmac
import json
import uuid

from fastapi import HTTPException, Request


def quote_context(quote, fixture) -> dict:
    return {key: quote[key] for key in ('market_kind', 'period', 'score_basis', 'play_id', 'market_instance_id', 'selection_id')} | {
        key: fixture[key] for key in ('phase', 'home_score', 'away_score', 'half_home_score', 'half_away_score')
    }


def authorize(request: Request, token: str) -> None:
    supplied = request.headers.get("authorization", "")
    if not token or not supplied.lower().startswith("bearer ") or not hmac.compare_digest(supplied[7:].strip(), token):
        raise HTTPException(status_code=401, detail="service authentication failed")


async def active_job(conn, review_id: uuid.UUID, attempt_id: uuid.UUID):
    job = await conn.fetchrow(
        "SELECT j.id,j.message_id,j.state,j.attempt_id,j.lease_until,j.review_mode,m.posted_at,m.raw_text,m.media_objects,m.revision,"
        "m.deleted_at,m.media_state,e.id AS expert_id,e.display_nickname,e.enabled AS expert_enabled,"
        "s.enabled AS source_enabled,se.enabled AS trusted "
        "FROM v2_analysis_jobs j JOIN v2_expert_messages m ON m.id=j.message_id "
        "JOIN v2_experts e ON e.id=m.expert_id JOIN v2_expert_sources s ON s.id=m.source_id "
        "LEFT JOIN v2_source_experts se ON se.source_id=m.source_id AND se.expert_id=m.expert_id "
        # Tool calls for one agent attempt are independently authenticated. A
        # shared lock keeps cancellation/completion from overtaking them while
        # allowing safe MCP calls in the same model batch to overlap.
        "WHERE j.id=$1 FOR SHARE OF j", review_id,
    )
    if not job or job["state"] != "running" or job["attempt_id"] != attempt_id or not job["lease_until"] or job["lease_until"] <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="review attempt is no longer active")
    if job["deleted_at"] or job["media_state"] != "ready" or not all((job["expert_enabled"], job["source_enabled"], job["trusted"])):
        raise HTTPException(status_code=409, detail="message or source is not eligible")
    latest = await conn.fetchval(
        "SELECT max(revision) FROM v2_expert_messages WHERE source_id=(SELECT source_id FROM v2_expert_messages WHERE id=$1) "
        "AND platform_message_id=(SELECT platform_message_id FROM v2_expert_messages WHERE id=$1)", job["message_id"],
    )
    if latest != job["revision"]:
        raise HTTPException(status_code=409, detail="message has a newer revision")
    return job


async def append_traces(conn, review_id: uuid.UUID, records: list[tuple[str, dict]]) -> None:
    if not records:
        return
    encoded = json.dumps([
        {"id": str(uuid.uuid4()), "trace_kind": kind, "payload": payload}
        for kind, payload in records
    ], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('v2-review-trace:' || $1::text,0))",
        str(review_id),
    )
    await conn.execute(
        "WITH sequence_base AS MATERIALIZED ("
        "SELECT COALESCE(MAX(t.sequence_no),0) AS last_sequence "
        "FROM v2_analysis_traces t WHERE t.job_id=$1),"
        "items AS ("
        "SELECT (entry.value->>'id')::uuid AS id,entry.value->>'trace_kind' AS trace_kind,"
        "entry.value->'payload' AS payload,entry.ordinality "
        "FROM jsonb_array_elements($2::jsonb) WITH ORDINALITY AS entry(value,ordinality)) "
        "INSERT INTO v2_analysis_traces(id,job_id,sequence_no,trace_kind,payload) "
        "SELECT items.id,$1,(sequence_base.last_sequence+items.ordinality)::integer,items.trace_kind,items.payload "
        "FROM items CROSS JOIN sequence_base",
        review_id, encoded,
    )


async def trace(conn, review_id: uuid.UUID, kind: str, payload: dict) -> None:
    await append_traces(conn, review_id, [(kind, payload)])


def fresh_quote(row, max_age: int, now: datetime | None = None) -> bool:
    stamp = row["source_updated_at"]
    if stamp is None or row["price"] is None or row["status"] != "active":
        return False
    age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
    return 0 <= age <= max_age
