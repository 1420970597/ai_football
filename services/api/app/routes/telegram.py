from __future__ import annotations

from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
import os
import re
from typing import Literal
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
import httpx
from pydantic import AwareDatetime, BaseModel, Field

from .. import database
from ..auth import audit, require_admin
from ..security import encrypt_secret
from .. import message_read
from .. import telegram_reconciliation
from ..review_policy import REALTIME_WINDOW_SECONDS


router = APIRouter()


def authorized(request: Request):
    expected = request.app.state.settings.collector_token
    actual = request.headers.get("authorization", "")
    if not expected or not hmac.compare_digest(actual.encode(), ("Bearer " + expected).encode()):
        raise HTTPException(status_code=401, detail="service authentication failed")


class ClaimInput(BaseModel):
    chat_id: str = Field(min_length=1, max_length=160)
    owner: uuid.UUID


class AdvanceInput(ClaimInput):
    last_message_id: int = Field(ge=0)
    previous_message_id: int = Field(ge=0)


class AuthorBackfillInput(ClaimInput):
    sender_id: str = Field(pattern=r'^[1-9][0-9]{0,18}$')
    previous_cursor: int = Field(ge=0)
    cursor: int = Field(ge=0)
    complete: bool = False


class ObserveInput(ClaimInput):
    reconciliation_protocol: Literal['claim-ack-v1']
    limit: int = Field(default=50, ge=1, le=100)


class ReconciliationReference(BaseModel):
    event_id: uuid.UUID
    version_key: str = Field(pattern="^[a-f0-9]{64}$")
    claim_id: uuid.UUID


class ReconciliationAckInput(ClaimInput):
    reconciliation: ReconciliationReference


class EventInput(ClaimInput):
    platform_message_id: str = Field(min_length=1, max_length=160)
    member_message_ids: list[int] = Field(min_length=1, max_length=50)
    version_key: str = Field(pattern="^[a-f0-9]{64}$")
    event_kind: str = Field(default="message", pattern="^(message|edit|delete)$")
    sender_id: str = Field(default="", max_length=160)
    sender_nickname: str = Field(default="", max_length=120)
    source_version_at: AwareDatetime
    posted_at: AwareDatetime
    raw_text: str = Field(default="", max_length=20000)
    reply_to_platform_message_id: str | None = Field(default=None, max_length=160)
    reply_context: dict = Field(default_factory=dict, max_length=10)
    media_state: str = Field(pattern="^(ready|pending|failed|unsupported)$")
    media_manifest: list[dict] = Field(default_factory=list, max_length=10)
    media_objects: list[dict] = Field(default_factory=list, max_length=10)
    last_error: str | None = Field(default=None, max_length=100)
    reconciliation: ReconciliationReference | None = None


class SessionInput(BaseModel):
    session: str = Field(min_length=50, max_length=4096)
    account: dict = Field(max_length=10)


class QRInput(BaseModel):
    replace_existing: bool = False


class PasswordInput(BaseModel):
    attempt_id: uuid.UUID
    password: str = Field(min_length=1, max_length=256)


class AttemptInput(BaseModel):
    attempt_id: uuid.UUID


class MediaOrphansInput(BaseModel):
    owner: uuid.UUID
    older_than_hours: int = Field(default=24, ge=1, le=720)
    limit: int = Field(default=100, ge=1, le=500)


class MediaOrphansAckInput(BaseModel):
    owner: uuid.UUID
    object_keys: list[str] = Field(min_length=1, max_length=500)


class ProfileInput(BaseModel):
    sender_id: str = Field(pattern=r'^-?[1-9][0-9]{0,18}$')
    nickname: str = Field(min_length=1, max_length=120)


class DirectoryInput(ClaimInput):
    observed_at: AwareDatetime
    state: str = Field(pattern='^(ready|unavailable|flood_wait)$')
    reason: str | None = Field(default=None, max_length=100)
    retry_after: int | None = Field(default=None, ge=0, le=864000)
    admins: list[dict] = Field(default_factory=list, max_length=2000)
    profiles: list[ProfileInput] = Field(default_factory=list, max_length=2000)


@router.put('/internal/telegram/directory')
async def sync_directory(body: DirectoryInput, request: Request):
    authorized(request)
    now = datetime.now(timezone.utc)
    if abs((now - body.observed_at).total_seconds()) > 120:
        raise HTTPException(422, '目录同步时间超出允许窗口')
    safe_admins = []
    seen = set()
    for item in body.admins:
        try:
            profile = ProfileInput.model_validate(item)
        except ValueError:
            raise HTTPException(422, '管理员身份或昵称无效') from None
        if profile.sender_id in seen or not profile.nickname.strip() or any(ord(c)<32 for c in profile.nickname):
            raise HTTPException(422, '管理员目录不完整或包含重复身份')
        seen.add(profile.sender_id)
        if item.get('admin_kind') not in {'owner', 'administrator'}:
            raise HTTPException(422, '管理员角色无效')
        safe_admins.append({'sender_id': profile.sender_id, 'nickname': profile.nickname.strip(),
            'username': str(item.get('username') or '')[:80], 'admin_kind': item['admin_kind'],
            'has_avatar': bool(item.get('has_avatar')), 'avatar_state': 'not_loaded' if item.get('has_avatar') else 'absent'})
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            source = await source_with_lease(conn, body.chat_id, body.owner)
            current = await conn.fetchval('SELECT last_attempt_at FROM v2_telegram_directories WHERE source_id=$1 FOR UPDATE', source['id'])
            if current and current >= body.observed_at:
                return {'persisted': False, 'state': 'stale'}
            await conn.execute(
                "INSERT INTO v2_telegram_directories(source_id,state,reason,retry_after,admins,last_attempt_at,last_success_at) "
                "VALUES($1,$2::text,$3,$4,$5,$6::timestamptz,CASE WHEN $2::text='ready' THEN $6::timestamptz ELSE NULL END) "
                "ON CONFLICT(source_id) DO UPDATE SET state=EXCLUDED.state,reason=EXCLUDED.reason,retry_after=EXCLUDED.retry_after,"
                "admins=CASE WHEN EXCLUDED.state='ready' THEN EXCLUDED.admins ELSE v2_telegram_directories.admins END,"
                "last_attempt_at=EXCLUDED.last_attempt_at,last_success_at=COALESCE(EXCLUDED.last_success_at,v2_telegram_directories.last_success_at)",
                source['id'], body.state, body.reason, body.retry_after, safe_admins, body.observed_at)
            updated = 0
            for profile in body.profiles:
                nickname = profile.nickname.strip()
                if not nickname or any(ord(c)<32 for c in nickname):
                    raise HTTPException(422, '专家昵称无效')
                expert = await conn.fetchrow(
                    'SELECT e.id,e.display_nickname FROM v2_experts e JOIN v2_source_experts se ON se.expert_id=e.id '
                    'WHERE se.source_id=$1 AND se.enabled AND e.enabled AND e.platform_sender_id=$2 FOR UPDATE OF e',
                    source['id'], profile.sender_id)
                if not expert:
                    continue
                for name in {expert['display_nickname'], nickname}:
                    await conn.execute(
                        'INSERT INTO v2_expert_nickname_history(expert_id,nickname,first_seen_at,last_seen_at) VALUES($1,$2,$3,$3) '
                        'ON CONFLICT(expert_id,nickname) DO UPDATE SET last_seen_at=EXCLUDED.last_seen_at', expert['id'], name, body.observed_at)
                if nickname != expert['display_nickname']:
                    await conn.execute('UPDATE v2_experts SET display_nickname=$2,updated_at=now() WHERE id=$1', expert['id'], nickname)
                    updated += 1
            return {'persisted': True, 'state': body.state, 'admins': len(safe_admins), 'nicknames_updated': updated}


async def source_for(conn, chat_id: str):
    row = await conn.fetchrow("SELECT id,allow_media FROM v2_expert_sources WHERE platform_chat_id=$1 AND enabled", chat_id)
    if not row:
        raise HTTPException(status_code=403, detail="Telegram source is not enabled")
    return row


async def source_with_lease(conn, chat_id: str, owner):
    row = await conn.fetchrow(
        "UPDATE v2_source_checkpoints cp SET lease_until=now()+interval '90 seconds' "
        "FROM v2_expert_sources s WHERE cp.source_id=s.id AND s.platform_chat_id=$1 AND s.enabled "
        "AND cp.collector_owner=$2 AND cp.lease_until>now() RETURNING s.id,s.allow_media",
        chat_id, owner,
    )
    if row:
        return row
    await source_for(conn, chat_id)
    raise HTTPException(status_code=409, detail="Telegram source lease is not active")


@router.get("/internal/telegram/sources")
async def collector_sources(request: Request):
    authorized(request)
    rows = await database.current_pool().fetch(
        "SELECT s.id,s.platform_chat_id AS chat_id,s.display_name,s.allow_media,"
        "COALESCE(array_agg(e.platform_sender_id) FILTER (WHERE se.enabled AND e.enabled),'{}') AS trusted_sender_ids,"
        "COALESCE(jsonb_agg(jsonb_build_object('sender_id',e.platform_sender_id,'cursor',se.backfill_cursor,"
        "'until_id',se.backfill_until_id,'since',se.backfill_since) ORDER BY se.added_at,e.id) "
        "FILTER (WHERE se.enabled AND e.enabled AND se.backfill_completed_at IS NULL "
        "AND se.backfill_since IS NOT NULL AND se.backfill_cursor>0 "
        "AND e.platform_sender_id ~ '^[1-9][0-9]{0,18}$'),'[]'::jsonb) AS author_backfills "
        "FROM v2_expert_sources s LEFT JOIN v2_source_experts se ON se.source_id=s.id "
        "LEFT JOIN v2_experts e ON e.id=se.expert_id WHERE s.enabled GROUP BY s.id ORDER BY s.id"
    )
    items = []
    for row in rows:
        item = dict(row)
        if isinstance(item['author_backfills'], str):
            item['author_backfills'] = json.loads(item['author_backfills'])
        items.append(item)
    return {"items": items}


@router.post("/internal/telegram/checkpoints/claim")
async def claim_checkpoint(body: ClaimInput, request: Request):
    authorized(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO v2_source_checkpoints(source_id,collector_owner,lease_until) "
                "SELECT s.id,$2,now()+interval '90 seconds' FROM v2_expert_sources s "
                "WHERE s.platform_chat_id=$1 AND s.enabled "
                "ON CONFLICT(source_id) DO UPDATE SET collector_owner=$2,lease_until=now()+interval '90 seconds' "
                "WHERE v2_source_checkpoints.collector_owner=$2 OR v2_source_checkpoints.lease_until IS NULL "
                "OR v2_source_checkpoints.lease_until<=now() RETURNING last_message_id,lease_until", body.chat_id, body.owner,
            )
            if not row:
                await source_for(conn, body.chat_id)
                raise HTTPException(status_code=409, detail="Telegram source is owned by another collector")
    return dict(row)


@router.put("/internal/telegram/checkpoints")
async def advance_checkpoint(body: AdvanceInput, request: Request):
    authorized(request)
    if body.last_message_id < body.previous_message_id:
        raise HTTPException(status_code=422, detail="checkpoint cannot move backwards")
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE v2_source_checkpoints cp SET last_message_id=$3,checked_at=now(),last_error=NULL,"
                "lease_until=now()+interval '90 seconds' FROM v2_expert_sources s "
                "WHERE cp.source_id=s.id AND s.platform_chat_id=$1 AND s.enabled "
                "AND cp.collector_owner=$2 AND cp.lease_until>now() AND cp.last_message_id=$4 "
                "RETURNING cp.last_message_id",
                body.chat_id, body.owner, body.last_message_id, body.previous_message_id,
            )
            if not row:
                source = await source_for(conn, body.chat_id)
                current = await conn.fetchrow(
                    "SELECT collector_owner,lease_until FROM v2_source_checkpoints WHERE source_id=$1",
                    source["id"],
                )
                if (not current or current["collector_owner"] != body.owner or not current["lease_until"]
                        or current["lease_until"] <= datetime.now(timezone.utc)):
                    raise HTTPException(status_code=409, detail="Telegram source lease is not active")
                raise HTTPException(status_code=409, detail="checkpoint has changed")
    return {"last_message_id": row["last_message_id"]}


@router.put('/internal/telegram/author-backfill')
async def advance_author_backfill(body: AuthorBackfillInput, request: Request):
    authorized(request)
    if body.cursor > body.previous_cursor or (body.cursor == body.previous_cursor and not body.complete):
        raise HTTPException(422, 'author backfill cursor must move backwards')
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            source = await source_with_lease(conn, body.chat_id, body.owner)
            mapping = await conn.fetchrow(
                "SELECT se.backfill_cursor,se.backfill_until_id FROM v2_source_experts se "
                "JOIN v2_experts e ON e.id=se.expert_id WHERE se.source_id=$1 AND e.platform_sender_id=$2 "
                "AND se.enabled AND e.enabled FOR UPDATE OF se,e", source['id'], body.sender_id)
            if not mapping:
                raise HTTPException(409, 'trusted author mapping is no longer active')
            if mapping['backfill_cursor'] != body.previous_cursor:
                raise HTTPException(409, 'author backfill cursor has changed')
            if body.cursor > mapping['backfill_until_id'] + 1:
                raise HTTPException(422, 'author backfill cursor exceeds its fixed upper bound')
            await conn.execute(
                "UPDATE v2_source_experts se SET backfill_cursor=$3,"
                "backfill_completed_at=CASE WHEN $4 THEN COALESCE(backfill_completed_at,now()) ELSE NULL END "
                "FROM v2_experts e WHERE se.source_id=$1 AND se.expert_id=e.id AND e.platform_sender_id=$2",
                source['id'], body.sender_id, body.cursor, body.complete)
    return {'cursor': body.cursor, 'complete': body.complete}


async def withdraw(conn, source_id, platform_ids: list[int], reason: str):
    rows = await conn.fetch(
        "SELECT DISTINCT e.message_id FROM v2_telegram_events e JOIN v2_telegram_event_members member ON member.event_id=e.id "
        "WHERE e.source_id=$1 AND member.platform_message_id=ANY($2::bigint[]) AND e.message_id IS NOT NULL",
        source_id, platform_ids,
    )
    message_ids = [row["message_id"] for row in rows]
    if message_ids:
        await conn.execute("UPDATE v2_expert_messages SET deleted_at=COALESCE(deleted_at,now()) WHERE id=ANY($1::uuid[])", message_ids)
        await conn.execute("UPDATE v2_analysis_jobs SET state='failed',last_error=$2,lease_until=NULL,attempt_id=NULL,updated_at=now() "
                           "WHERE message_id=ANY($1::uuid[]) AND state IN ('queued','retry','running')", message_ids, reason)
        await conn.execute("UPDATE v2_expert_signals SET withdrawn_at=COALESCE(withdrawn_at,now()),withdrawal_reason=COALESCE(withdrawal_reason,$2) "
                           "WHERE message_id=ANY($1::uuid[])", message_ids, reason)


@router.post("/internal/telegram/events")
async def ingest_event(body: EventInput, request: Request):
    authorized(request)
    if any(value <= 0 for value in body.member_message_ids):
        raise HTTPException(status_code=422, detail="invalid Telegram message identifier")
    if body.media_state == "ready" and body.media_manifest and len(body.media_objects) != len(body.media_manifest):
        raise HTTPException(status_code=422, detail="all message media must be uploaded before review")
    for item in body.media_objects:
        if not all(item.get(key) for key in ("object_key", "mime_type", "sha256")) or ".." in item["object_key"].split("/"):
            raise HTTPException(status_code=422, detail="invalid remote media evidence")
    pool = database.current_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            source = await source_with_lease(conn, body.chat_id, body.owner)
            if body.reconciliation is not None:
                payload = jsonable_encoder(body.model_dump(exclude={'owner', 'reconciliation'}))
                duplicate = await telegram_reconciliation.apply_claim(
                    conn, source['id'], body.owner, body.reconciliation,
                    platform_message_id=body.platform_message_id, member_message_ids=body.member_message_ids,
                    event_kind=body.event_kind,
                    payload_sha256=hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                )
                if duplicate:
                    return {"persisted": True, "state": "reconciled", "duplicate": True}
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"telegram:{source['id']}:{body.platform_message_id}")
            if body.event_kind == "delete":
                affected = await conn.fetch(
                    "SELECT DISTINCT all_members.platform_message_id FROM v2_telegram_events e "
                    "JOIN v2_telegram_event_members all_members ON all_members.event_id=e.id "
                    "WHERE e.source_id=$1 AND e.id IN (SELECT event_id FROM v2_telegram_event_members "
                    "WHERE platform_message_id=ANY($2::bigint[]))", source["id"], body.member_message_ids,
                )
                deleted_ids = sorted(set(body.member_message_ids) | {row["platform_message_id"] for row in affected})
                await conn.executemany("INSERT INTO v2_telegram_tombstones(source_id,platform_message_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
                                       [(source["id"], item) for item in deleted_ids])
                await withdraw(conn, source["id"], body.member_message_ids, "source_deleted")
                await conn.execute("UPDATE v2_telegram_events SET is_current=FALSE WHERE source_id=$1 AND id IN "
                                   "(SELECT event_id FROM v2_telegram_event_members WHERE platform_message_id=ANY($2::bigint[]))",
                                   source["id"], body.member_message_ids)
                return {"persisted": True, "state": "deleted"}
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM v2_telegram_tombstones WHERE source_id=$1 AND platform_message_id=ANY($2::bigint[]))",
                                   source["id"], body.member_message_ids):
                return {"persisted": True, "state": "deleted"}
            expert = await conn.fetchrow(
                "SELECT e.id FROM v2_experts e JOIN v2_source_experts se ON se.expert_id=e.id "
                "WHERE e.platform_sender_id=$1 AND e.enabled AND se.source_id=$2 AND se.enabled", body.sender_id, source["id"],
            )
            if not expert:
                raise HTTPException(status_code=403, detail="sender is not trusted for this source")
            if not body.sender_nickname.strip() or (not body.raw_text.strip() and not body.media_manifest):
                raise HTTPException(status_code=422, detail="message content and author are required")
            if body.media_manifest and not source["allow_media"] and body.media_state != "unsupported":
                raise HTTPException(status_code=403, detail="source media is disabled")
            current = await conn.fetchrow(
                "SELECT * FROM v2_telegram_events WHERE source_id=$1 AND platform_message_id=$2 AND is_current FOR UPDATE",
                source["id"], body.platform_message_id,
            )
            event = await conn.fetchrow("SELECT * FROM v2_telegram_events WHERE source_id=$1 AND platform_message_id=$2 AND version_key=$3",
                                        source["id"], body.platform_message_id, body.version_key)
            if event and not event["is_current"]:
                return {"persisted": True, "state": "superseded", "message_id": str(event["message_id"])}
            if not event and current and current["source_version_at"] > body.source_version_at:
                return {"persisted": True, "state": "ignored_stale"}
            content = jsonable_encoder(body.model_dump(exclude={"owner", "media_objects", "media_state", "last_error", "reconciliation"}))
            media_objects = body.media_objects
            if event and body.media_state in {'pending', 'failed'} and not media_objects:
                # History staging/retry must not erase candidates registered
                # before an S3 timeout; orphan inventory relies on these keys.
                media_objects = event['media_objects']
            immutable_content = {key: value for key, value in content.items() if key != "sender_nickname"}
            if event and {key: value for key, value in event["content"].items() if key != "sender_nickname"} != immutable_content:
                raise HTTPException(status_code=409, detail="same message version has conflicting content")
            await conn.execute("UPDATE v2_experts SET display_nickname=$2,updated_at=now() WHERE id=$1 AND display_nickname IS DISTINCT FROM $2", expert["id"], body.sender_nickname.strip())
            if event and event["media_state"] == "ready":
                return {"persisted": True, "state": "ready", "message_id": str(event["message_id"])}
            if not event:
                if current:
                    await withdraw(conn, source["id"], body.member_message_ids, "source_edited")
                    await conn.execute("UPDATE v2_telegram_events SET is_current=FALSE WHERE id=$1", current["id"])
                revision = await conn.fetchval(
                    "SELECT COALESCE(MAX(revision),0)+1 FROM v2_expert_messages WHERE source_id=$1 AND platform_message_id=$2",
                    source["id"], body.platform_message_id,
                )
                message_id, event_id = uuid.uuid4(), uuid.uuid4()
                await conn.execute(
                    "INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,reply_to_platform_message_id,raw_text,"
                    "media_objects,posted_at,revision,content_hash,media_state,edited_at,reply_context) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                    message_id, source["id"], expert["id"], body.platform_message_id, body.reply_to_platform_message_id,
                    body.raw_text, body.media_objects, body.posted_at, revision, body.version_key, body.media_state,
                    body.source_version_at if body.event_kind == "edit" else None, body.reply_context,
                )
                await conn.execute(
                    "INSERT INTO v2_telegram_events(id,source_id,platform_message_id,version_key,source_version_at,event_kind,content,"
                    "media_objects,media_state,message_id,last_error) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
                    event_id, source["id"], body.platform_message_id, body.version_key, body.source_version_at, body.event_kind,
                    content, body.media_objects, body.media_state, message_id, body.last_error,
                )
                await conn.executemany("INSERT INTO v2_telegram_event_members(event_id,platform_message_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
                                       [(event_id, item) for item in body.member_message_ids])
            else:
                message_id, event_id = event["message_id"], event["id"]
                await conn.execute("UPDATE v2_telegram_events SET media_objects=$2,media_state=$3,last_error=$4,updated_at=now() WHERE id=$1",
                                   event_id, media_objects, body.media_state, body.last_error)
                await conn.execute("UPDATE v2_expert_messages SET media_objects=$2,media_state=$3 WHERE id=$1", message_id, media_objects, body.media_state)
            job_id = None
            if body.media_state == "ready":
                age = (datetime.now(timezone.utc) - body.posted_at.astimezone(timezone.utc)).total_seconds()
                auto_review = 0 <= age <= REALTIME_WINDOW_SECONDS
                review_mode = 'real_time' if auto_review or age < 0 else 'historical_replay'
                initial_state = 'queued' if auto_review else 'unresolved'
                suppression = None if auto_review else (
                    'historical_backlog_auto_suppressed_by_operator' if age > REALTIME_WINDOW_SECONDS
                    else 'message_timestamp_in_future')
                idempotency_key = hashlib.sha256(
                    f"telegram:{source['id']}:{body.platform_message_id}:{body.version_key}".encode()).hexdigest()
                job_id = await conn.fetchval(
                    "INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,review_mode,last_error) "
                    "VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(idempotency_key) DO NOTHING RETURNING id",
                    uuid.uuid4(), message_id, initial_state, idempotency_key, review_mode, suppression,
                )
                if job_id and suppression:
                    await conn.execute(
                        "INSERT INTO v2_analysis_traces(id,job_id,sequence_no,trace_kind,payload) "
                        "VALUES($1,$2,1,'telegram_auto_review_suppressed',$3)",
                        uuid.uuid4(), job_id, {'reason': suppression, 'review_mode': review_mode,
                            'posted_at': body.posted_at.astimezone(timezone.utc).isoformat(),
                            'realtime_window_seconds': REALTIME_WINDOW_SECONDS,
                            'source_message_preserved': True, 'media_preserved': True},
                    )
                if not job_id:
                    job_id = await conn.fetchval(
                        "SELECT id FROM v2_analysis_jobs WHERE idempotency_key=$1", idempotency_key)
    return {"persisted": True, "state": body.media_state, "message_id": str(message_id),
            "review_id": str(job_id) if job_id else None,
            "media_objects": media_objects if body.media_state in {'pending', 'failed'} else []}


@router.get("/internal/telegram/pending")
async def pending_events(request: Request, limit: int = 30):
    authorized(request)
    rows = await database.current_pool().fetch(
        "SELECT e.platform_message_id,e.content,e.media_objects,e.last_error,s.platform_chat_id AS chat_id FROM v2_telegram_events e "
        "JOIN v2_expert_sources s ON s.id=e.source_id WHERE e.is_current AND s.enabled AND e.media_state IN ('pending','failed') "
        "ORDER BY CASE WHEN (e.content->>'posted_at')::timestamptz >= now()-($2::int*interval '1 second') "
        "THEN 0 ELSE 1 END,CASE WHEN e.media_state='failed' THEN 1 ELSE 0 END,"
        "CASE WHEN e.media_state='pending' AND (e.content->>'posted_at')::timestamptz >= now()-($2::int*interval '1 second') "
        "THEN (e.content->>'posted_at')::timestamptz END DESC,e.updated_at,e.id LIMIT $1",
        max(1, min(limit, 100)), REALTIME_WINDOW_SECONDS,
    )
    return {"items": [dict(row) for row in rows]}


def _valid_media_key(value: str) -> bool:
    return bool(value and len(value) <= 1024 and not value.startswith("/") and ".." not in value.split("/"))


@router.post("/internal/telegram/media/orphans")
async def media_orphans(body: MediaOrphansInput, request: Request):
    authorized(request)
    async with database.current_pool().acquire() as conn:
        rows = await conn.fetch(
            "WITH candidates AS ("
            "SELECT e.id,e.source_id,s.platform_chat_id AS chat_id,e.updated_at,obj->>'object_key' AS object_key "
            "FROM v2_telegram_events e JOIN v2_expert_sources s ON s.id=e.source_id "
            "JOIN v2_source_checkpoints cp ON cp.source_id=e.source_id "
            "CROSS JOIN LATERAL jsonb_array_elements(e.media_objects) obj "
            "WHERE e.media_state IN ('pending','failed') "
            "AND e.updated_at < now()-($2::int * interval '1 hour') "
            "AND cp.collector_owner=$1 AND cp.lease_until>now() ),"
            "ready_refs AS ("
            "SELECT obj->>'object_key' AS object_key FROM v2_telegram_events e "
            "CROSS JOIN LATERAL jsonb_array_elements(e.media_objects) obj "
            "WHERE e.is_current AND e.media_state='ready' "
            "UNION ALL SELECT obj->>'object_key' AS object_key FROM v2_expert_messages m "
            "CROSS JOIN LATERAL jsonb_array_elements(m.media_objects) obj "
            "WHERE m.media_state='ready' AND m.deleted_at IS NULL ) "
            "SELECT DISTINCT ON (c.object_key) c.id,c.chat_id,c.object_key,c.updated_at "
            "FROM candidates c LEFT JOIN ready_refs r ON r.object_key=c.object_key "
            "WHERE c.object_key IS NOT NULL AND r.object_key IS NULL "
            "ORDER BY c.object_key,c.updated_at LIMIT $3",
            body.owner, body.older_than_hours, body.limit,
        )
    return {"items": [dict(row) for row in rows], "state": "ready"}


@router.post("/internal/telegram/media/orphans/ack")
async def acknowledge_media_orphans(body: MediaOrphansAckInput, request: Request):
    authorized(request)
    keys = [str(value).strip() for value in body.object_keys]
    if any(not _valid_media_key(value) for value in keys):
        raise HTTPException(status_code=422, detail="invalid remote media object reference")
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "UPDATE v2_telegram_events e SET media_objects=COALESCE(("
                "SELECT jsonb_agg(obj) FROM jsonb_array_elements(e.media_objects) obj "
                "WHERE COALESCE(obj->>'object_key','') <> ALL($2::text[])), '[]'::jsonb),"
                "last_error=COALESCE(e.last_error,'media_orphan_reclaimed'),updated_at=now() "
                "FROM v2_source_checkpoints cp WHERE cp.source_id=e.source_id "
                "AND cp.collector_owner=$1 AND cp.lease_until>now() "
                "AND e.media_state IN ('pending','failed') AND EXISTS ("
                "SELECT 1 FROM jsonb_array_elements(e.media_objects) obj "
                "WHERE obj->>'object_key'=ANY($2::text[]))",
                body.owner, keys,
            )
    return {"acknowledged": int(result.rsplit(" ", 1)[-1]), "object_keys": len(keys)}


@router.post("/internal/telegram/observed")
async def observed_events(body: ObserveInput, request: Request):
    authorized(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            source = await source_with_lease(conn, body.chat_id, body.owner)
            rows = await telegram_reconciliation.claim_events(conn, source['id'], body.owner, body.limit)
    return {"items": rows, "claim_lease_seconds": telegram_reconciliation.RECONCILIATION_LEASE_SECONDS}


@router.post("/internal/telegram/observed/ack")
async def acknowledge_observed_event(body: ReconciliationAckInput, request: Request):
    authorized(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            source = await source_with_lease(conn, body.chat_id, body.owner)
            return await telegram_reconciliation.acknowledge_claim(conn, source['id'], body.owner, body.reconciliation)


@router.put("/internal/telegram/session")
async def persist_session(body: SessionInput, request: Request):
    authorized(request)
    safe_account = {key: body.account.get(key) for key in ("id", "display_name", "username")}
    key = request.app.state.settings.encryption_key
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            for setting, value in (("telegram_session", body.session), ("telegram_account", json.dumps(safe_account)), ("telegram_source_enabled", "true")):
                await conn.execute(
                    "INSERT INTO v2_admin_settings(setting_key,encrypted_value) VALUES($1,$2) "
                    "ON CONFLICT(setting_key) DO UPDATE SET encrypted_value=EXCLUDED.encrypted_value,updated_at=now()",
                    setting, encrypt_secret(key, value),
                )
            await audit(conn, None, "service", "telegram_session_authorized", "telegram_account", str(safe_account.get("id", "")))
    return {"persisted": True, "account": safe_account}


async def control(request: Request, path: str, payload: dict | None = None):
    admin = await require_admin(request)
    url = os.environ.get("TELEGRAM_CONTROL_URL", "http://collector:8092").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(28, connect=3), follow_redirects=False,
                                     headers={"Authorization": "Bearer " + request.app.state.settings.collector_token}) as client:
            response = await client.post(url + path, json=payload or {})
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="Telegram 控制服务暂不可用，请稍后重试") from None
    if path not in {"/status", "/dialogs", "/admins", "/members"}:
        async with database.current_pool().acquire() as conn:
            await audit(conn, admin["id"], "admin", "telegram_control_" + path.rsplit("/", 1)[-1], "telegram_account",
                        str((payload or {}).get("attempt_id", "")), getattr(request.state, "request_id", ""))
    return result


@router.get("/api/v1/admin/telegram/status")
async def telegram_status(request: Request):
    return await control(request, "/status")


@router.post("/api/v1/admin/telegram/auth/qr")
async def qr_start(body: QRInput, request: Request):
    return await control(request, "/auth/qr", body.model_dump())


@router.post("/api/v1/admin/telegram/auth/password")
async def qr_password(body: PasswordInput, request: Request):
    return await control(request, "/auth/password", jsonable_encoder(body))


@router.post("/api/v1/admin/telegram/auth/cancel")
async def qr_cancel(body: AttemptInput, request: Request):
    return await control(request, "/auth/cancel", jsonable_encoder(body))


@router.post("/api/v1/admin/telegram/disconnect")
async def telegram_disconnect(request: Request):
    result = await control(request, "/disconnect")
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM v2_admin_settings WHERE setting_key='telegram_session'")
            await conn.execute("INSERT INTO v2_admin_settings(setting_key,encrypted_value) VALUES('telegram_source_enabled',$1) "
                               "ON CONFLICT(setting_key) DO UPDATE SET encrypted_value=EXCLUDED.encrypted_value,updated_at=now()",
                               encrypt_secret(request.app.state.settings.encryption_key, "false"))
    return result


@router.get("/api/v1/admin/telegram/dialogs")
async def telegram_dialogs(request: Request, cursor: str | None = Query(default=None, max_length=7),
                           q: str = Query(default="", max_length=160)):
    if any(ord(char) < 32 for char in q):
        raise HTTPException(422, "搜索内容不能包含控制字符")
    return await directory_control(request, "/dialogs", {"cursor": directory_cursor(cursor), "q": q.strip()})


@router.get("/api/v1/admin/telegram/admins")
async def telegram_admins(request: Request, chat_id: str = Query(min_length=1, max_length=160),
                          cursor: str | None = Query(default=None, max_length=7), q: str = Query(default="", max_length=160)):
    chat_id = chat_id.strip()
    if not (re.fullmatch(r"-[1-9][0-9]{0,18}", chat_id) or re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", chat_id)):
        raise HTTPException(422, "请使用负数稳定群组编号或 @频道用户名，不能使用群名或个人账号编号")
    if chat_id.startswith("-") and int(chat_id) < -(2 ** 63):
        raise HTTPException(422, "群组编号超出支持范围")
    if any(ord(char) < 32 for char in q):
        raise HTTPException(422, "搜索内容不能包含控制字符")
    return await directory_control(request, "/admins", {"chat_id": chat_id, "cursor": directory_cursor(cursor), "q": q.strip()})


def directory_cursor(cursor):
    if cursor is not None and (not re.fullmatch(r"[0-9]{1,7}", cursor) or int(cursor) > 1_000_000):
        raise HTTPException(422, "无效的 Telegram 目录分页游标")
    return cursor


@router.get('/api/v1/admin/telegram/members')
async def telegram_members(request: Request, chat_id: str = Query(min_length=1, max_length=160),
                           cursor: str | None = Query(default=None, max_length=7), q: str = Query(default='', max_length=160)):
    chat_id = chat_id.strip()
    if not (re.fullmatch(r'-[1-9][0-9]{0,18}', chat_id) or re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{4,31}', chat_id)):
        raise HTTPException(422, '请使用负数稳定群组编号或 @频道用户名')
    if chat_id.startswith('-') and int(chat_id) < -(2**63):
        raise HTTPException(422, '群组编号超出支持范围')
    if any(ord(char)<32 for char in q):
        raise HTTPException(422, '搜索内容不能包含控制字符')
    return await directory_control(request, '/members', {'chat_id':chat_id, 'cursor':directory_cursor(cursor), 'q':q.strip()})


async def directory_control(request, path, payload):
    result = await control(request, path, payload)
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise HTTPException(503, "Telegram 目录服务返回格式无效，请稍后重试")
    # Existing web consumers accept 'available'. Keep the private collector's
    # readiness vocabulary separate from this documented public API contract.
    if result.get("state") == "ready":
        result = dict(result, state="available")
    return result


@router.get("/api/v1/admin/telegram/messages")
async def telegram_messages(request: Request, expert_id: uuid.UUID | None = None, cursor: str | None = None, limit: int = 30):
    await require_admin(request)
    before, before_id = None, None
    if cursor:
        try:
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            payload, signature = raw[:-32], raw[-32:]
            expected = hmac.new(request.app.state.settings.encryption_key, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError
            scope, stamp, identifier = json.loads(payload)
            if scope != (str(expert_id) if expert_id else "all"):
                raise ValueError
            before, before_id = datetime.fromisoformat(stamp), uuid.UUID(identifier)
            if before.tzinfo is None:
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(status_code=400, detail="消息分页游标无效") from None
    page_size = max(1, min(limit, 100))
    rows = await database.current_pool().fetch(
        "SELECT m.id,m.raw_text,m.media_objects,m.media_state,m.posted_at,m.edited_at,m.revision,m.deleted_at,"
        "e.display_nickname,m.expert_id,m.source_id,m.platform_message_id,t.last_error "
        "FROM v2_expert_messages m JOIN v2_experts e ON e.id=m.expert_id LEFT JOIN v2_telegram_events t ON t.message_id=m.id "
        "WHERE ($1::uuid IS NULL OR m.expert_id=$1) AND ($2::timestamptz IS NULL OR (m.posted_at,m.id)<($2,$3)) "
        "ORDER BY m.posted_at DESC,m.id DESC LIMIT $4", expert_id, before, before_id, page_size + 1,
    )
    page, next_cursor = rows[:page_size], None
    if len(rows)>page_size:
        payload = json.dumps([str(expert_id) if expert_id else "all", page[-1]["posted_at"].isoformat(), str(page[-1]["id"])]).encode()
        signature = hmac.new(request.app.state.settings.encryption_key, payload, hashlib.sha256).digest()
        next_cursor = base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")
    return {"items": [dict(row) for row in page], "next_cursor": next_cursor}


@router.get("/api/v1/admin/telegram/messages/{message_id}")
async def telegram_message_detail(message_id: uuid.UUID, request: Request):
    actor = await message_read.reader(request, admin=True)
    pool = database.current_pool()
    message = await pool.fetchrow(
        "SELECT m.*,e.display_nickname,t.last_error,t.content AS source_evidence FROM v2_expert_messages m "
        "JOIN v2_experts e ON e.id=m.expert_id LEFT JOIN LATERAL(SELECT last_error,content FROM v2_telegram_events "
        "WHERE message_id=m.id ORDER BY received_at DESC,id DESC LIMIT 1) t ON TRUE WHERE m.id=$1", message_id,
    )
    if not message:
        raise message_read.private_error(404, "消息原文不存在")
    return JSONResponse(jsonable_encoder(await message_read.detail(pool, request, actor, message, context='admin')),
                        headers=message_read.HEADERS)


@router.get('/api/v1/admin/telegram/messages/{message_id}/traces')
async def telegram_message_traces(message_id: uuid.UUID, request: Request,
                                  limit: int = Query(default=30, ge=1, le=100),
                                  cursor: str | None = Query(default=None, max_length=1000)):
    actor = await message_read.reader(request, admin=True)
    pool = database.current_pool()
    if not await pool.fetchval('SELECT EXISTS(SELECT 1 FROM v2_expert_messages WHERE id=$1)', message_id):
        raise message_read.private_error(404, '消息原文不存在')
    return JSONResponse(jsonable_encoder(await message_read.trace_page(pool, request, actor, message_id,
                        context='admin', limit=limit, cursor=cursor)), headers=message_read.HEADERS)


@router.get('/api/v1/admin/telegram/messages/{message_id}/signals')
async def telegram_message_signals(message_id: uuid.UUID, request: Request,
                                   limit: int = Query(default=30, ge=1, le=100),
                                   cursor: str | None = Query(default=None, max_length=1000)):
    actor = await message_read.reader(request, admin=True)
    pool = database.current_pool()
    if not await pool.fetchval('SELECT EXISTS(SELECT 1 FROM v2_expert_messages WHERE id=$1)', message_id):
        raise message_read.private_error(404, '消息原文不存在')
    return JSONResponse(jsonable_encoder(await message_read.signal_page(pool, request, actor, message_id,
                        context='admin', limit=limit, cursor=cursor)), headers=message_read.HEADERS)


@router.get('/api/v1/admin/telegram/messages/{message_id}/tools/{trace_id}')
async def telegram_message_tool_response(message_id: uuid.UUID, trace_id: uuid.UUID, request: Request):
    await message_read.reader(request, admin=True)
    return JSONResponse(jsonable_encoder(await message_read.tool_response(database.current_pool(), message_id, trace_id)),
                        headers=message_read.HEADERS)


@router.get('/api/v1/admin/telegram/messages/{message_id}/media/{index}')
async def telegram_message_media(message_id: uuid.UUID, index: int, request: Request):
    actor = await message_read.reader(request, admin=True)
    from .expert_content import read_message_media
    return await read_message_media(message_id, index, request=request, actor=actor, context='admin')
