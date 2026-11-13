from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Annotated, Literal
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
import httpx
from pydantic import AwareDatetime, BaseModel, Field, field_validator

from .. import database
from ..auth import audit, require_admin
from ..security import encrypt_secret
from .. import message_read
from .. import telegram_reconciliation
from ..telegram_profiles import profile_updates, sync_trusted_profiles
from ..review_policy import REALTIME_WINDOW_SECONDS, FRESH_PRIORITY_WINDOW_SECONDS


router = APIRouter()
log = logging.getLogger('platform.api')


def log_version_conflict(stored: dict, incoming: dict, *, path: str) -> None:
    """Report schema fields only; never emit message values or identifiers."""
    allowed = EventInput.model_fields.keys() - {'owner', 'sender_nickname'}
    fields = sorted(key for key in allowed if stored.get(key) != incoming.get(key)
                    or (key in stored) != (key in incoming))
    log.warning('Telegram event version conflict path=%s fields=%s', path, ','.join(fields) or 'none')


async def expired_event_receipt(conn, source_id, body):
    # Older collectors may have uploaded before discovering expiry. Keep the
    # existing orphan cleanup path without recreating an original or review.
    if body.media_objects:
        await conn.execute('SELECT v2_queue_retention_media($1,$2)', source_id, body.media_objects)
    return {'persisted': True, 'state': 'expired', 'reason': 'raw_content_expired'}


def record_event_phase(request: Request, name: str, started: float) -> None:
    phases = getattr(request.state, "telegram_event_phases", {})
    phases[name] = round((time.perf_counter() - started) * 1000, 1)
    request.state.telegram_event_phases = phases


@asynccontextmanager
async def event_phase_scope(request: Request, name: str):
    started = time.perf_counter()
    try:
        yield
    finally:
        record_event_phase(request, name, started)


def authorized(request: Request):
    expected = request.app.state.settings.collector_token
    actual = request.headers.get("authorization", "")
    if not expected or not hmac.compare_digest(actual.encode(), ("Bearer " + expected).encode()):
        raise HTTPException(status_code=401, detail="service authentication failed")


class ClaimInput(BaseModel):
    chat_id: str = Field(min_length=1, max_length=160)
    owner: uuid.UUID


class ReleaseInput(BaseModel):
    owner: uuid.UUID


class AdvanceInput(ClaimInput):
    last_message_id: int = Field(ge=0)
    previous_message_id: int = Field(ge=0)


class AuthorBackfillInput(ClaimInput):
    sender_id: str = Field(pattern=r'^-?[1-9][0-9]{0,18}$')
    previous_cursor: int = Field(ge=0)
    cursor: int = Field(ge=0)
    complete: bool = False

    @field_validator('sender_id')
    @classmethod
    def stable_sender_range(cls, value):
        if not -(2 ** 63) <= int(value) < 2 ** 63:
            raise ValueError('sender identity is outside signed 64-bit range')
        return value


class ObserveInput(ClaimInput):
    reconciliation_protocol: Literal['claim-ack-v1']
    limit: int = Field(default=50, ge=1, le=100)
    claim_request_id: uuid.UUID = Field(default_factory=uuid.uuid4)


class EventReceiptsInput(ClaimInput):
    member_message_ids: list[Annotated[int, Field(strict=True, gt=0)]] = Field(min_length=1, max_length=100)


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
    detected_at: AwareDatetime | None = None
    raw_text: str = Field(default="", max_length=20000)
    reply_to_platform_message_id: str | None = Field(default=None, max_length=160)
    reply_context: dict = Field(default_factory=dict, max_length=10)
    media_state: str = Field(pattern="^(ready|pending|failed|unsupported)$")
    media_manifest: list[dict] = Field(default_factory=list, max_length=10)
    media_objects: list[dict] = Field(default_factory=list, max_length=10)
    last_error: str | None = Field(default=None, max_length=100)
    reconciliation: ReconciliationReference | None = None

    @field_validator('detected_at')
    @classmethod
    def detection_cannot_extend_budget_into_the_future(cls, value):
        if value is not None and value > datetime.now(timezone.utc):
            raise ValueError('detected_at must not be in the future')
        return value


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
    try:
        profiles = profile_updates(body.profiles)
    except ValueError:
        raise HTTPException(422, '专家昵称无效或同一作者存在冲突昵称') from None
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
        if item.get('admin_kind') not in {'owner', 'administrator', 'channel'}:
            raise HTTPException(422, '管理员角色无效')
        if item['admin_kind'] == 'channel' and (profile.sender_id != body.chat_id
                                               or not profile.sender_id.startswith('-')):
            raise HTTPException(422, '频道身份须为该来源自身的稳定编号')
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
            updated = await sync_trusted_profiles(conn, source['id'], profiles, body.observed_at)
            return {'persisted': True, 'state': body.state, 'admins': len(safe_admins), 'nicknames_updated': updated}


async def source_for(conn, chat_id: str):
    row = await conn.fetchrow("SELECT id,allow_media FROM v2_expert_sources WHERE platform_chat_id=$1 AND enabled", chat_id)
    if not row:
        raise HTTPException(status_code=403, detail="Telegram source is not enabled")
    return row


async def source_with_lease(conn, chat_id: str, owner, claim_request_id: uuid.UUID | None = None):
    request_lock_cte = ""
    request_lock_join = ""
    args = (chat_id, owner)
    if claim_request_id is not None:
        request_lock_cte = (
            ",request_lock AS MATERIALIZED ("
            "SELECT pg_advisory_xact_lock(hashtextextended('telegram-observed:' || platform_chat_id || ':' || "
            "$2::uuid::text || ':' || $3::uuid::text,0)) AS acquired FROM source)"
        )
        request_lock_join = " CROSS JOIN request_lock"
        args = (chat_id, owner, claim_request_id)
    row = await conn.fetchrow(
        "WITH source AS MATERIALIZED ("
        "SELECT id,allow_media,platform_chat_id FROM v2_expert_sources "
        "WHERE platform_chat_id=$1 AND enabled),"
        "source_lock AS MATERIALIZED ("
        "SELECT pg_advisory_xact_lock_shared(hashtext('telegram-source:' || platform_chat_id)) AS acquired "
        "FROM source)"
        f"{request_lock_cte},"
        "locked_source AS MATERIALIZED ("
        f"SELECT source.id,source.allow_media FROM source CROSS JOIN source_lock{request_lock_join}) "
        "SELECT locked_source.id,locked_source.allow_media FROM locked_source "
        "JOIN v2_source_checkpoints cp ON cp.source_id=locked_source.id "
        "WHERE cp.collector_owner=$2 AND cp.lease_until>now()",
        *args,
    )
    if row:
        return row
    await source_for(conn, chat_id)
    raise HTTPException(status_code=409, detail="Telegram source lease is not active")


async def source_and_message_lock(conn, chat_id: str, platform_message_id: str):
    """Acquire source then message locks in one round trip; validate lease afterwards."""
    return await conn.fetchrow(
        "WITH source AS MATERIALIZED ("
        " SELECT id,allow_media,platform_chat_id FROM v2_expert_sources "
        " WHERE platform_chat_id=$1 AND enabled),"
        "source_lock AS MATERIALIZED ("
        " SELECT pg_advisory_xact_lock_shared(hashtext('telegram-source:' || platform_chat_id)) AS acquired "
        " FROM source),"
        "locked_source AS MATERIALIZED ("
        " SELECT source.id,source.allow_media FROM source CROSS JOIN source_lock),"
        "message_lock AS MATERIALIZED ("
        " SELECT pg_advisory_xact_lock(hashtext('telegram:' || locked_source.id::text || ':' || $2)) AS acquired "
        " FROM locked_source) "
        "SELECT locked_source.id,locked_source.allow_media FROM locked_source CROSS JOIN message_lock",
        chat_id, platform_message_id,
    )


async def event_ingest_snapshot(conn, body: EventInput, source_id, owner):
    """Read the event state after the caller has acquired the message lock."""
    row = await conn.fetchrow(
        "WITH source_state AS MATERIALIZED ("
        " SELECT s.enabled,(cp.collector_owner=$6 AND cp.lease_until>statement_timestamp()) AS lease_active "
        " FROM v2_expert_sources s LEFT JOIN v2_source_checkpoints cp ON cp.source_id=s.id WHERE s.id=$1),"
        "tombstoned AS MATERIALIZED ("
        " SELECT EXISTS(SELECT 1 FROM v2_telegram_tombstones t "
        " WHERE t.source_id=$1 AND t.platform_message_id=ANY($4::bigint[])) AS value),"
        "expert AS MATERIALIZED ("
        " SELECT e.id FROM v2_experts e JOIN v2_source_experts se ON se.expert_id=e.id "
        " WHERE e.platform_sender_id=$3 AND e.enabled AND se.source_id=$1 AND se.enabled),"
        "current_event AS MATERIALIZED ("
        " SELECT e.id,e.source_version_at FROM v2_telegram_events e "
        " WHERE e.source_id=$1 AND e.platform_message_id=$2 AND e.is_current),"
        "version_event AS MATERIALIZED ("
        " SELECT e.id,e.is_current,e.message_id,e.media_state,e.media_objects,e.content,e.raw_content_expired_at "
        " FROM v2_telegram_events e "
        " WHERE e.source_id=$1 AND e.platform_message_id=$2 AND e.version_key=$5),"
        "revision AS MATERIALIZED ("
        " SELECT COALESCE(MAX(m.revision),0)+1 AS value FROM v2_expert_messages m "
        " WHERE m.source_id=$1 AND m.platform_message_id=$2) "
        "SELECT COALESCE((SELECT enabled FROM source_state),FALSE) AS source_enabled,"
        "COALESCE((SELECT lease_active FROM source_state),FALSE) AS lease_active,"
        "(SELECT id FROM expert) AS expert_id,COALESCE((SELECT value FROM tombstoned),FALSE) AS tombstoned,"
        "(SELECT id FROM current_event) AS current_id,"
        "(SELECT source_version_at FROM current_event) AS current_source_version_at,"
        "(SELECT id FROM version_event) AS event_id,(SELECT is_current FROM version_event) AS event_is_current,"
        "(SELECT message_id FROM version_event) AS event_message_id,"
        "(SELECT media_state FROM version_event) AS event_media_state,"
        "(SELECT media_objects FROM version_event) AS event_media_objects,"
        "(SELECT content FROM version_event) AS event_content,"
        "(SELECT raw_content_expired_at FROM version_event) AS event_expired_at,(SELECT value FROM revision) AS next_revision",
        source_id, body.platform_message_id, body.sender_id, body.member_message_ids, body.version_key, owner,
    )
    event = None
    if row["event_id"] is not None:
        event = {
            "id": row["event_id"], "is_current": row["event_is_current"],
            "message_id": row["event_message_id"], "media_state": row["event_media_state"],
            "media_objects": row["event_media_objects"], "content": row["event_content"],
            "raw_content_expired_at": row['event_expired_at'],
        }
    return {
        "source_enabled": row["source_enabled"], "lease_active": row["lease_active"],
        "expert": {"id": row["expert_id"]} if row["expert_id"] is not None else None,
        "tombstoned": row["tombstoned"],
        "current": ({"id": row["current_id"], "source_version_at": row["current_source_version_at"]}
                    if row["current_id"] is not None else None),
        "event": event, "next_revision": row["next_revision"],
    }


def review_job_metadata(source_id, body: EventInput):
    age = (datetime.now(timezone.utc) - body.posted_at.astimezone(timezone.utc)).total_seconds()
    source_peer = body.sender_id == body.chat_id and body.sender_id.startswith('-')
    # Newly trusted channel identities recover bounded history independently
    # of the main checkpoint. Retain old posts for explicit review rather than
    # flooding the real-time queue with hours of previously unseen messages.
    realtime_window = min(REALTIME_WINDOW_SECONDS, FRESH_PRIORITY_WINDOW_SECONDS) if source_peer else REALTIME_WINDOW_SECONDS
    auto_review = 0 <= age <= realtime_window
    review_mode = 'real_time' if auto_review or age < 0 else 'historical_replay'
    initial_state = 'queued' if auto_review else 'unresolved'
    suppression = None if auto_review else (
        'historical_backlog_auto_suppressed_by_operator' if age > realtime_window
        else 'message_timestamp_in_future')
    idempotency_key = hashlib.sha256(
        f"telegram:{source_id}:{body.platform_message_id}:{body.version_key}".encode()).hexdigest()
    return {
        'job_id': uuid.uuid4(), 'trace_id': uuid.uuid4(), 'initial_state': initial_state,
        'review_mode': review_mode, 'suppression': suppression, 'idempotency_key': idempotency_key,
        'trace_payload': ({'reason': suppression, 'review_mode': review_mode,
                           'posted_at': body.posted_at.astimezone(timezone.utc).isoformat(),
                           'realtime_window_seconds': realtime_window,
                           'configured_realtime_window_seconds': REALTIME_WINDOW_SECONDS,
                           'source_peer_history': source_peer,
                           'source_message_preserved': True, 'media_preserved': True}
                          if suppression else None),
    }


async def persist_fast_event(conn, source_id, body: EventInput):
    """Take a fresh post-lock snapshot and apply the live event atomically."""
    content = jsonable_encoder(body.model_dump(
        exclude={"owner", "media_objects", "media_state", "last_error", "reconciliation", "detected_at"}))
    body_payload = jsonable_encoder(body.model_dump(exclude={"reconciliation"}))
    content_payload = content
    job = review_job_metadata(source_id, body) if body.media_state == "ready" else None
    job_payload = jsonable_encoder(job) if job else None
    row = await conn.fetchrow(
        "WITH input AS MATERIALIZED ("
        " SELECT $1::uuid AS source_id,$2::jsonb AS body,$3::jsonb AS content,$4::jsonb AS job,"
        " $5::uuid AS message_id,$6::uuid AS event_id),"
        "source_state AS MATERIALIZED ("
        " SELECT s.enabled,s.allow_media,(cp.collector_owner=(i.body->>'owner')::uuid "
        " AND cp.lease_until>statement_timestamp()) AS lease_active "
        " FROM input i JOIN v2_expert_sources s ON s.id=i.source_id "
        " LEFT JOIN v2_source_checkpoints cp ON cp.source_id=s.id),"
        "tombstoned AS MATERIALIZED ("
        " SELECT EXISTS(SELECT 1 FROM v2_telegram_tombstones t CROSS JOIN input i "
        " WHERE t.source_id=i.source_id AND t.platform_message_id=ANY("
        " ARRAY(SELECT jsonb_array_elements_text(i.body->'member_message_ids')::bigint))) AS value),"
        "expert AS MATERIALIZED ("
        " SELECT e.id FROM input i JOIN v2_experts e ON e.platform_sender_id=i.body->>'sender_id' AND e.enabled "
        " JOIN v2_source_experts se ON se.expert_id=e.id AND se.source_id=i.source_id AND se.enabled),"
        "current_event AS MATERIALIZED ("
        " SELECT e.id,e.source_version_at FROM input i JOIN v2_telegram_events e ON e.source_id=i.source_id "
        " AND e.platform_message_id=i.body->>'platform_message_id' AND e.is_current),"
        "version_event AS MATERIALIZED ("
        " SELECT e.id,e.is_current,e.message_id,e.media_state,e.media_objects,e.content,e.raw_content_expired_at "
        " FROM input i JOIN v2_telegram_events e ON e.source_id=i.source_id "
        " AND e.platform_message_id=i.body->>'platform_message_id' AND e.version_key=i.body->>'version_key'),"
        "revision AS MATERIALIZED ("
        " SELECT COALESCE(MAX(m.revision),0)+1 AS value FROM input i JOIN v2_expert_messages m "
        " ON m.source_id=i.source_id AND m.platform_message_id=i.body->>'platform_message_id'),"
        "decision AS MATERIALIZED ("
        " SELECT i.source_id,i.body,i.content,i.job,ss.allow_media,x.id AS expert_id,"
        " COALESCE(ss.enabled,FALSE) AS source_enabled,COALESCE(ss.lease_active,FALSE) AS lease_active,"
        " COALESCE(t.value,FALSE) AS tombstoned,c.id AS current_id,c.source_version_at AS current_version_at,"
        " v.id AS version_id,v.is_current AS version_is_current,v.message_id AS version_message_id,"
        " v.media_state AS version_media_state,v.media_objects AS version_media_objects,v.content AS version_content,"
        " r.value AS next_revision,CASE "
        " WHEN NOT COALESCE(ss.enabled,FALSE) THEN 'source_disabled' "
        " WHEN NOT COALESCE(ss.lease_active,FALSE) THEN 'lease_inactive' "
        " WHEN COALESCE(t.value,FALSE) THEN 'deleted' "
        " WHEN x.id IS NULL THEN 'untrusted' "
        " WHEN NULLIF(btrim(i.body->>'sender_nickname'),'') IS NULL OR "
        " (NULLIF(btrim(i.body->>'raw_text'),'') IS NULL AND "
        " COALESCE(jsonb_array_length(i.body->'media_manifest'),0)=0) THEN 'invalid_content' "
        " WHEN COALESCE(jsonb_array_length(i.body->'media_manifest'),0)>0 "
        " AND NOT COALESCE(ss.allow_media,FALSE) AND i.body->>'media_state'<>'unsupported' THEN 'media_disabled' "
        " WHEN v.id IS NOT NULL AND NOT v.is_current THEN 'superseded' "
        " WHEN (i.body->>'posted_at')::timestamptz<statement_timestamp()-interval '72 hours' "
        " OR v.raw_content_expired_at IS NOT NULL THEN 'expired' "
        " WHEN v.id IS NOT NULL AND (v.content-'sender_nickname') IS DISTINCT FROM "
        " (i.content-'sender_nickname') THEN 'version_conflict' "
        " WHEN v.id IS NULL AND c.source_version_at>(i.body->>'source_version_at')::timestamptz THEN 'ignored_stale' "
        " WHEN v.id IS NOT NULL AND v.media_state='ready' THEN 'ready' ELSE 'accepted' END AS outcome "
        " FROM input i LEFT JOIN source_state ss ON TRUE LEFT JOIN tombstoned t ON TRUE "
        " LEFT JOIN expert x ON TRUE LEFT JOIN current_event c ON TRUE LEFT JOIN version_event v ON TRUE "
        " CROSS JOIN revision r),"
        "nickname_update AS ("
        " UPDATE v2_experts e SET display_nickname=btrim(d.body->>'sender_nickname'),updated_at=now() "
        " FROM decision d WHERE e.id=d.expert_id AND d.outcome IN ('accepted','ready') "
        " AND e.display_nickname IS DISTINCT FROM btrim(d.body->>'sender_nickname') RETURNING e.id),"
        "previous_messages AS MATERIALIZED ("
        " SELECT DISTINCT e.message_id FROM decision d JOIN v2_telegram_events e ON e.source_id=d.source_id "
        " JOIN v2_telegram_event_members member ON member.event_id=e.id "
        " WHERE d.outcome='accepted' AND d.version_id IS NULL AND d.current_id IS NOT NULL "
        " AND e.message_id IS NOT NULL AND member.platform_message_id=ANY("
        " ARRAY(SELECT jsonb_array_elements_text(d.body->'member_message_ids')::bigint))),"
        "withdrawn_messages AS ("
        " UPDATE v2_expert_messages m SET deleted_at=COALESCE(m.deleted_at,now()) "
        " WHERE m.id IN (SELECT message_id FROM previous_messages) RETURNING m.id),"
        "withdrawn_jobs AS ("
        " UPDATE v2_analysis_jobs j SET state='failed',last_error='source_edited',lease_until=NULL,"
        " attempt_id=NULL,updated_at=now() WHERE j.message_id IN (SELECT message_id FROM previous_messages) "
        " AND j.state IN ('queued','retry','running') RETURNING j.id),"
        "withdrawn_signals AS ("
        " UPDATE v2_expert_signals s SET withdrawn_at=COALESCE(s.withdrawn_at,now()),"
        " withdrawal_reason=COALESCE(s.withdrawal_reason,'source_edited') "
        " WHERE s.message_id IN (SELECT message_id FROM previous_messages) RETURNING s.id),"
        "superseded_event AS ("
        " UPDATE v2_telegram_events e SET is_current=FALSE FROM decision d "
        " WHERE e.id=d.current_id AND d.outcome='accepted' AND d.version_id IS NULL "
        " AND d.current_id IS NOT NULL RETURNING e.id),"
        "event_gate AS MATERIALIZED ("
        " SELECT d.* FROM decision d WHERE d.outcome='accepted' AND "
        " (d.version_id IS NOT NULL OR d.current_id IS NULL OR EXISTS(SELECT 1 FROM superseded_event))),"
        "inserted_message AS ("
        " INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,reply_to_platform_message_id,raw_text,"
        " media_objects,posted_at,revision,content_hash,media_state,edited_at,reply_context,detected_at) "
        " SELECT i.message_id,i.source_id,d.expert_id,d.body->>'platform_message_id',"
        " NULLIF(d.body->>'reply_to_platform_message_id',''),d.body->>'raw_text',d.body->'media_objects',"
        " (d.body->>'posted_at')::timestamptz,d.next_revision,d.body->>'version_key',d.body->>'media_state',"
        " CASE WHEN d.body->>'event_kind'='edit' THEN (d.body->>'source_version_at')::timestamptz END,"
        " d.body->'reply_context',(d.body->>'detected_at')::timestamptz FROM event_gate d JOIN input i ON i.source_id=d.source_id "
        " WHERE d.version_id IS NULL RETURNING id),"
        "inserted_event AS ("
        " INSERT INTO v2_telegram_events(id,source_id,platform_message_id,version_key,source_version_at,event_kind,content,"
        " media_objects,media_state,message_id,last_error) "
        " SELECT i.event_id,d.source_id,d.body->>'platform_message_id',d.body->>'version_key',"
        " (d.body->>'source_version_at')::timestamptz,d.body->>'event_kind',d.content,d.body->'media_objects',"
        " d.body->>'media_state',m.id,NULLIF(d.body->>'last_error','') FROM event_gate d "
        " JOIN input i ON i.source_id=d.source_id JOIN inserted_message m ON TRUE RETURNING id),"
        "inserted_members AS ("
        " INSERT INTO v2_telegram_event_members(event_id,platform_message_id) "
        " SELECT e.id,member.member_id::bigint FROM inserted_event e CROSS JOIN decision d "
        " CROSS JOIN LATERAL jsonb_array_elements_text(d.body->'member_message_ids') AS member(member_id) "
        " ON CONFLICT DO NOTHING RETURNING event_id),"
        "updated_event AS ("
        " UPDATE v2_telegram_events e SET media_objects=CASE "
        " WHEN d.body->>'media_state' IN ('pending','failed') AND d.body->'media_objects'='[]'::jsonb "
        " THEN d.version_media_objects ELSE d.body->'media_objects' END,"
        " media_state=d.body->>'media_state',last_error=NULLIF(d.body->>'last_error',''),updated_at=now() "
        " FROM decision d WHERE e.id=d.version_id AND d.outcome='accepted' AND d.version_id IS NOT NULL "
        " RETURNING e.id,e.message_id,e.media_objects),"
        "updated_message AS ("
        " UPDATE v2_expert_messages m SET media_objects=e.media_objects,media_state=d.body->>'media_state' "
        " FROM updated_event e CROSS JOIN decision d WHERE m.id=e.message_id RETURNING m.id),"
        "message_target AS MATERIALIZED ("
        " SELECT id FROM inserted_message UNION ALL SELECT id FROM updated_message),"
        "inserted_job AS ("
        " INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,review_mode,last_error) "
        " SELECT (i.job->>'job_id')::uuid,target.id,i.job->>'initial_state',i.job->>'idempotency_key',"
        " i.job->>'review_mode',i.job->>'suppression' FROM message_target target CROSS JOIN input i "
        " WHERE i.job IS NOT NULL AND i.job<>'null'::jsonb "
        " ON CONFLICT(idempotency_key) DO NOTHING RETURNING id),"
        "resolved_job AS ("
        " SELECT id FROM inserted_job UNION ALL SELECT j.id FROM input i JOIN v2_analysis_jobs j "
        " ON j.idempotency_key=i.job->>'idempotency_key' WHERE i.job IS NOT NULL AND i.job<>'null'::jsonb "
        " AND NOT EXISTS(SELECT 1 FROM inserted_job) LIMIT 1),"
        "suppression_trace AS ("
        " INSERT INTO v2_analysis_traces(id,job_id,sequence_no,trace_kind,payload) "
        " SELECT (i.job->>'trace_id')::uuid,j.id,1,'telegram_auto_review_suppressed',i.job->'trace_payload' "
        " FROM inserted_job j CROSS JOIN input i WHERE i.job->>'suppression' IS NOT NULL RETURNING id) "
        "SELECT d.outcome,COALESCE((SELECT id FROM inserted_message),(SELECT message_id FROM updated_event),"
        " d.version_message_id) AS message_id,(SELECT id FROM resolved_job) AS job_id,"
        "CASE WHEN d.version_id IS NOT NULL AND d.body->>'media_state' IN ('pending','failed') "
        " AND d.body->'media_objects'='[]'::jsonb THEN d.version_media_objects ELSE d.body->'media_objects' END AS media_objects "
        " ,CASE WHEN d.outcome='version_conflict' THEN d.version_content END AS conflicting_content "
        "FROM decision d",
        source_id, body_payload, content_payload, job_payload, uuid.uuid4(), uuid.uuid4(),
    )
    result = dict(row)
    conflicting_content = result.pop('conflicting_content', None)
    if result['outcome'] == 'version_conflict':
        log_version_conflict(conflicting_content or {}, content, path='live')
    return result


async def insert_event_message_and_members(conn, *, message_id, event_id, source_id, expert_id,
                                           revision: int, body: EventInput, content: dict,
                                           nickname: str, previous_event_id, job_metadata):
    job = job_metadata or {}
    row = await conn.fetchrow(
        "WITH nickname_update AS ("
        " UPDATE v2_experts SET display_nickname=$20,updated_at=now() "
        " WHERE id=$4 AND display_nickname IS DISTINCT FROM $20 RETURNING id),"
        "previous_messages AS MATERIALIZED ("
        " SELECT DISTINCT e.message_id FROM v2_telegram_events e "
        " JOIN v2_telegram_event_members member ON member.event_id=e.id "
        " WHERE $21::uuid IS NOT NULL AND e.source_id=$3 AND e.message_id IS NOT NULL "
        " AND member.platform_message_id=ANY($19::bigint[])),"
        "withdrawn_messages AS ("
        " UPDATE v2_expert_messages m SET deleted_at=COALESCE(m.deleted_at,now()) "
        " WHERE m.id IN (SELECT message_id FROM previous_messages) RETURNING m.id),"
        "withdrawn_jobs AS ("
        " UPDATE v2_analysis_jobs j SET state='failed',last_error='source_edited',lease_until=NULL,"
        " attempt_id=NULL,updated_at=now() WHERE j.message_id IN (SELECT message_id FROM previous_messages) "
        " AND j.state IN ('queued','retry','running') RETURNING j.id),"
        "withdrawn_signals AS ("
        " UPDATE v2_expert_signals s SET withdrawn_at=COALESCE(s.withdrawn_at,now()),"
        " withdrawal_reason=COALESCE(s.withdrawal_reason,'source_edited') "
        " WHERE s.message_id IN (SELECT message_id FROM previous_messages) RETURNING s.id),"
        "superseded_event AS ("
        " UPDATE v2_telegram_events SET is_current=FALSE WHERE id=$21 AND is_current RETURNING id),"
        "event_gate AS ("
        " SELECT 1 AS ready WHERE $21::uuid IS NULL UNION ALL SELECT 1 FROM superseded_event),"
        "inserted_message AS ("
        " INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,reply_to_platform_message_id,raw_text,"
        " media_objects,posted_at,revision,content_hash,media_state,edited_at,reply_context,detected_at) "
        " SELECT $1,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$29 FROM event_gate RETURNING id),"
        "inserted_event AS ("
        " INSERT INTO v2_telegram_events(id,source_id,platform_message_id,version_key,source_version_at,event_kind,content,"
        " media_objects,media_state,message_id,last_error) "
        " SELECT $2,$3,$5,$11,$15,$16,$17,$8,$12,inserted_message.id,$18 FROM inserted_message "
        " RETURNING id),"
        "inserted_members AS ("
        " INSERT INTO v2_telegram_event_members(event_id,platform_message_id) "
        " SELECT inserted_event.id,members.member_id FROM inserted_event "
        " CROSS JOIN unnest($19::bigint[]) AS members(member_id) "
        " ON CONFLICT DO NOTHING RETURNING event_id),"
        "inserted_job AS ("
        " INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,review_mode,last_error) "
        " SELECT $27,inserted_message.id,$23,$22,$24,$25 FROM inserted_message "
        " WHERE $22::text IS NOT NULL ON CONFLICT(idempotency_key) DO NOTHING RETURNING id),"
        "resolved_job AS ("
        " SELECT id FROM inserted_job UNION ALL "
        " SELECT j.id FROM v2_analysis_jobs j WHERE $22::text IS NOT NULL AND j.idempotency_key=$22 "
        " AND NOT EXISTS(SELECT 1 FROM inserted_job) LIMIT 1),"
        "suppression_trace AS ("
        " INSERT INTO v2_analysis_traces(id,job_id,sequence_no,trace_kind,payload) "
        " SELECT $28,inserted_job.id,1,'telegram_auto_review_suppressed',$26 FROM inserted_job "
        " WHERE $25::text IS NOT NULL RETURNING id) "
        "SELECT (SELECT id FROM inserted_message) AS message_id,(SELECT id FROM inserted_event) AS event_id,"
        "(SELECT count(*) FROM inserted_members) AS member_count,(SELECT id FROM resolved_job) AS job_id",
        message_id, event_id, source_id, expert_id, body.platform_message_id,
        body.reply_to_platform_message_id, body.raw_text, body.media_objects, body.posted_at,
        revision, body.version_key, body.media_state,
        body.source_version_at if body.event_kind == "edit" else None, body.reply_context,
        body.source_version_at, body.event_kind, content, body.last_error, body.member_message_ids,
        nickname, previous_event_id, job.get('idempotency_key'), job.get('initial_state'),
        job.get('review_mode'), job.get('suppression'), job.get('trace_payload'),
        job.get('job_id'), job.get('trace_id'), body.detected_at,
    )
    if not row['message_id'] or not row['event_id']:
        raise RuntimeError("Telegram event insert lost its current-version fence")
    return row


async def update_event_media_state(conn, *, event_id, message_id, media_objects, state, last_error,
                                   expert_id, nickname: str, job_metadata):
    job = job_metadata or {}
    row = await conn.fetchrow(
        "WITH nickname_update AS ("
        " UPDATE v2_experts SET display_nickname=$7,updated_at=now() "
        " WHERE id=$6 AND display_nickname IS DISTINCT FROM $7 RETURNING id),"
        "updated_event AS ("
        " UPDATE v2_telegram_events SET media_objects=$3,media_state=$4,last_error=$5,updated_at=now() "
        " WHERE id=$1 RETURNING id),"
        "updated_message AS ("
        " UPDATE v2_expert_messages m SET media_objects=$3,media_state=$4 "
        " FROM updated_event e WHERE m.id=$2 AND e.id=$1 RETURNING m.id),"
        "inserted_job AS ("
        " INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,review_mode,last_error) "
        " SELECT $12,updated_message.id,$9,$8,$10,$11 FROM updated_message "
        " WHERE $8::text IS NOT NULL ON CONFLICT(idempotency_key) DO NOTHING RETURNING id),"
        "resolved_job AS ("
        " SELECT id FROM inserted_job UNION ALL "
        " SELECT j.id FROM v2_analysis_jobs j WHERE $8::text IS NOT NULL AND j.idempotency_key=$8 "
        " AND NOT EXISTS(SELECT 1 FROM inserted_job) LIMIT 1),"
        "suppression_trace AS ("
        " INSERT INTO v2_analysis_traces(id,job_id,sequence_no,trace_kind,payload) "
        " SELECT $13,inserted_job.id,1,'telegram_auto_review_suppressed',$14 FROM inserted_job "
        " WHERE $11::text IS NOT NULL RETURNING id) "
        "SELECT (SELECT id FROM updated_message) AS message_id,(SELECT id FROM resolved_job) AS job_id",
        event_id, message_id, media_objects, state, last_error, expert_id, nickname,
        job.get('idempotency_key'), job.get('initial_state'), job.get('review_mode'),
        job.get('suppression'), job.get('job_id'), job.get('trace_id'), job.get('trace_payload'),
    )
    if not row or row['message_id'] != message_id:
        raise RuntimeError("Telegram event media state update was incomplete")
    return row


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
        "AND (e.platform_sender_id ~ '^[1-9][0-9]{0,18}$' "
        "OR e.platform_sender_id=s.platform_chat_id)),'[]'::jsonb) AS author_backfills "
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
                "WITH source AS MATERIALIZED ("
                "SELECT id,platform_chat_id FROM v2_expert_sources WHERE platform_chat_id=$1 AND enabled),"
                "current AS MATERIALIZED ("
                "SELECT cp.source_id,cp.collector_owner,cp.lease_until FROM v2_source_checkpoints cp "
                "JOIN source ON source.id=cp.source_id),"
                "lock_candidate AS MATERIALIZED ("
                "SELECT source.platform_chat_id FROM source LEFT JOIN current ON current.source_id=source.id "
                "WHERE current.source_id IS NULL OR (current.collector_owner<>$2 "
                "AND (current.lease_until IS NULL OR current.lease_until<=now()))),"
                "source_lock AS MATERIALIZED ("
                "SELECT pg_advisory_xact_lock(hashtext('telegram-source:' || platform_chat_id)) "
                "FROM lock_candidate),"
                "claim_sources AS MATERIALIZED ("
                "SELECT source.id FROM source CROSS JOIN (SELECT count(*) FROM source_lock) lock_guard) "
                "INSERT INTO v2_source_checkpoints(source_id,collector_owner,lease_until) "
                "SELECT claim_sources.id,$2,now()+interval '90 seconds' FROM claim_sources "
                "ON CONFLICT(source_id) DO UPDATE SET collector_owner=$2,lease_until=now()+interval '90 seconds' "
                "WHERE v2_source_checkpoints.collector_owner=$2 OR v2_source_checkpoints.lease_until IS NULL "
                "OR v2_source_checkpoints.lease_until<=now() RETURNING last_message_id,lease_until", body.chat_id, body.owner,
            )
            if not row:
                await source_for(conn, body.chat_id)
                raise HTTPException(status_code=409, detail="Telegram source is owned by another collector")
    return dict(row)


@router.post("/internal/telegram/checkpoints/release")
async def release_checkpoints(body: ReleaseInput, request: Request):
    authorized(request)
    released = await database.current_pool().fetchval(
        "WITH released AS (UPDATE v2_source_checkpoints SET lease_until=clock_timestamp() "
        "WHERE collector_owner=$1 AND lease_until>clock_timestamp() RETURNING 1) "
        "SELECT count(*) FROM released", body.owner,
    )
    return {"released_sources": released}


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


@router.post('/internal/telegram/events/receipts')
async def event_receipts(body: EventReceiptsInput, request: Request):
    """Bounded durable confirmations, never an ID-only deduplication claim."""
    authorized(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction(readonly=True):
            source = await source_with_lease(conn, body.chat_id, body.owner)
            rows = await conn.fetch(
                "SELECT e.version_key,e.media_state,e.message_id,e.content->>'sender_nickname' AS sender_nickname "
                "FROM v2_telegram_events e JOIN v2_expert_messages m ON m.id=e.message_id "
                "JOIN v2_experts x ON x.id=m.expert_id AND x.enabled "
                "JOIN v2_source_experts se ON se.expert_id=x.id AND se.source_id=e.source_id AND se.enabled "
                "WHERE e.source_id=$1 AND e.is_current AND m.deleted_at IS NULL "
                "AND EXISTS(SELECT 1 FROM v2_telegram_event_members em WHERE em.event_id=e.id "
                "AND em.platform_message_id=ANY($2::bigint[])) ORDER BY e.id LIMIT 101",
                source['id'], body.member_message_ids,
            )
            if len(rows) > 100:
                raise HTTPException(413, 'Telegram receipt budget exceeded; no partial response returned')
    return {'items': [dict(row) for row in rows]}


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
    if body.reconciliation is not None:
        event_path = "reconciliation_delete" if body.event_kind == "delete" else "reconciliation"
    else:
        event_path = "delete" if body.event_kind == "delete" else "live"
    request.state.telegram_event_path = event_path
    request.state.telegram_event_phases = {}
    request.state.telegram_event_accounted_phases = ("pool_wait_ms", "transaction_ms")
    pool = database.current_pool()
    pool_wait_started = time.perf_counter()
    async with pool.acquire() as conn:
        record_event_phase(request, "pool_wait_ms", pool_wait_started)
        async with event_phase_scope(request, "transaction_ms"), conn.transaction():
            if body.reconciliation is None and body.event_kind != "delete":
                lock_started = time.perf_counter()
                try:
                    source = await source_and_message_lock(conn, body.chat_id, body.platform_message_id)
                finally:
                    record_event_phase(request, "source_message_lock_ms", lock_started)
                if not source:
                    await source_for(conn, body.chat_id)
                    raise HTTPException(status_code=403, detail="Telegram source is not enabled")
                persist_started = time.perf_counter()
                try:
                    result = await persist_fast_event(conn, source["id"], body)
                finally:
                    record_event_phase(request, "event_sql_ms", persist_started)
                outcome = result["outcome"]
                if outcome == "source_disabled":
                    raise HTTPException(status_code=403, detail="Telegram source is not enabled")
                if outcome == "lease_inactive":
                    raise HTTPException(status_code=409, detail="Telegram source lease is not active")
                if outcome == "deleted":
                    return {"persisted": True, "state": "deleted"}
                if outcome == "untrusted":
                    raise HTTPException(status_code=403, detail="sender is not trusted for this source")
                if outcome == "invalid_content":
                    raise HTTPException(status_code=422, detail="message content and author are required")
                if outcome == "media_disabled":
                    raise HTTPException(status_code=403, detail="source media is disabled")
                if outcome == "superseded":
                    return {"persisted": True, "state": "superseded", "message_id": str(result["message_id"])}
                if outcome == 'expired':
                    return await expired_event_receipt(conn, source['id'], body)
                if outcome == "ignored_stale":
                    return {"persisted": True, "state": "ignored_stale"}
                if outcome == "version_conflict":
                    raise HTTPException(status_code=409, detail="same message version has conflicting content")
                if outcome == "ready":
                    return {"persisted": True, "state": "ready", "message_id": str(result["message_id"])}
                if outcome != "accepted" or not result["message_id"]:
                    raise RuntimeError("Telegram event persistence returned no valid outcome")
                return {"persisted": True, "state": body.media_state,
                        "message_id": str(result["message_id"]),
                        "review_id": str(result["job_id"]) if result["job_id"] else None,
                        "media_objects": result["media_objects"]
                        if body.media_state in {"pending", "failed"} else []}

            source = await source_with_lease(conn, body.chat_id, body.owner)
            async def acknowledge_reconciliation():
                if body.reconciliation is None:
                    return {}
                result = await telegram_reconciliation.acknowledge_claim(
                    conn, source['id'], body.owner, body.reconciliation)
                return {'reconciliation_acknowledged': result['acknowledged'],
                        'reconciliation_ack_duplicate': result['duplicate']}

            # Match the live path's source -> message -> event lock order.
            # apply_claim locks its event row; taking it before the message
            # lock can deadlock a concurrent pending-media completion.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))",
                               f"telegram:{source['id']}:{body.platform_message_id}")
            if body.reconciliation is not None:
                payload = jsonable_encoder(body.model_dump(exclude={'owner', 'reconciliation', 'detected_at'}))
                duplicate = await telegram_reconciliation.apply_claim(
                    conn, source['id'], body.owner, body.reconciliation,
                    platform_message_id=body.platform_message_id, member_message_ids=body.member_message_ids,
                    event_kind=body.event_kind,
                    payload_sha256=hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                )
                if duplicate:
                    ack = await acknowledge_reconciliation()
                    return {"persisted": True, "state": "reconciled", "duplicate": True, **ack}
            snapshot = None
            if body.event_kind != "delete":
                # A new statement after the advisory-lock wait is essential:
                # the lease or message version may have changed while waiting.
                # Read the reconciliation state in one remote round trip.
                async with event_phase_scope(request, "event_snapshot_ms"):
                    snapshot = await event_ingest_snapshot(conn, body, source['id'], body.owner)
                if not snapshot['source_enabled']:
                    raise HTTPException(status_code=403, detail="Telegram source is not enabled")
                if not snapshot['lease_active']:
                    raise HTTPException(status_code=409, detail="Telegram source lease is not active")
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
                ack = await acknowledge_reconciliation()
                return {"persisted": True, "state": "deleted", **ack}
            deleted = (snapshot["tombstoned"] if snapshot is not None else await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM v2_telegram_tombstones WHERE source_id=$1 AND platform_message_id=ANY($2::bigint[]))",
                source["id"], body.member_message_ids))
            if deleted:
                ack = await acknowledge_reconciliation()
                return {"persisted": True, "state": "deleted", **ack}
            expert = (snapshot["expert"] if snapshot is not None else await conn.fetchrow(
                "SELECT e.id FROM v2_experts e JOIN v2_source_experts se ON se.expert_id=e.id "
                "WHERE e.platform_sender_id=$1 AND e.enabled AND se.source_id=$2 AND se.enabled", body.sender_id, source["id"],
            ))
            if not expert:
                raise HTTPException(status_code=403, detail="sender is not trusted for this source")
            if not body.sender_nickname.strip() or (not body.raw_text.strip() and not body.media_manifest):
                raise HTTPException(status_code=422, detail="message content and author are required")
            if body.media_manifest and not source["allow_media"] and body.media_state != "unsupported":
                raise HTTPException(status_code=403, detail="source media is disabled")
            if snapshot is not None:
                current, event = snapshot["current"], snapshot["event"]
            else:
                current = await conn.fetchrow(
                    "SELECT * FROM v2_telegram_events WHERE source_id=$1 AND platform_message_id=$2 AND is_current FOR UPDATE",
                    source["id"], body.platform_message_id,
                )
                event = await conn.fetchrow(
                    "SELECT * FROM v2_telegram_events WHERE source_id=$1 AND platform_message_id=$2 AND version_key=$3",
                    source["id"], body.platform_message_id, body.version_key)
            if event and not event["is_current"]:
                ack = await acknowledge_reconciliation()
                return {"persisted": True, "state": "superseded", "message_id": str(event["message_id"]), **ack}
            if (body.posted_at < datetime.now(timezone.utc)-timedelta(hours=72)
                    or event and event.get('raw_content_expired_at') is not None):
                expired = await expired_event_receipt(conn, source['id'], body)
                return {**expired, **(await acknowledge_reconciliation())}
            if not event and current and current["source_version_at"] > body.source_version_at:
                ack = await acknowledge_reconciliation()
                return {"persisted": True, "state": "ignored_stale", **ack}
            content = jsonable_encoder(body.model_dump(exclude={"owner", "media_objects", "media_state", "last_error", "reconciliation", "detected_at"}))
            media_objects = body.media_objects
            if event and body.media_state in {'pending', 'failed'} and not media_objects:
                # History staging/retry must not erase candidates registered
                # before an S3 timeout; orphan inventory relies on these keys.
                media_objects = event['media_objects']
            immutable_content = {key: value for key, value in content.items() if key != "sender_nickname"}
            if event and {key: value for key, value in event["content"].items() if key != "sender_nickname"} != immutable_content:
                log_version_conflict(event['content'], content, path='reconciliation')
                raise HTTPException(status_code=409, detail="same message version has conflicting content")
            if event and event["media_state"] == "ready":
                await conn.execute("UPDATE v2_experts SET display_nickname=$2,updated_at=now() WHERE id=$1 AND display_nickname IS DISTINCT FROM $2", expert["id"], body.sender_nickname.strip())
                ack = await acknowledge_reconciliation()
                return {"persisted": True, "state": "ready", "message_id": str(event["message_id"]), **ack}
            if not event:
                revision = (snapshot["next_revision"] if snapshot is not None else await conn.fetchval(
                    "SELECT COALESCE(MAX(revision),0)+1 FROM v2_expert_messages WHERE source_id=$1 AND platform_message_id=$2",
                    source["id"], body.platform_message_id,
                ))
                message_id, event_id = uuid.uuid4(), uuid.uuid4()
                job_metadata = review_job_metadata(source['id'], body) if body.media_state == 'ready' else None
                inserted = await insert_event_message_and_members(
                    conn, message_id=message_id, event_id=event_id, source_id=source["id"],
                    expert_id=expert["id"], revision=revision, body=body, content=content,
                    nickname=body.sender_nickname.strip(), previous_event_id=current['id'] if current else None,
                    job_metadata=job_metadata)
                job_id = inserted['job_id']
            else:
                message_id, event_id = event["message_id"], event["id"]
                job_metadata = review_job_metadata(source['id'], body) if body.media_state == 'ready' else None
                updated = await update_event_media_state(
                    conn, event_id=event_id, message_id=message_id, media_objects=media_objects,
                    state=body.media_state, last_error=body.last_error, expert_id=expert['id'],
                    nickname=body.sender_nickname.strip(), job_metadata=job_metadata)
                job_id = updated['job_id']
            ack = await acknowledge_reconciliation()
    return {"persisted": True, "state": body.media_state, "message_id": str(message_id),
            "review_id": str(job_id) if job_id else None,
            "media_objects": media_objects if body.media_state in {'pending', 'failed'} else [], **ack}


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
    # The existing collector gives its sequential S3 delete/ack pass five
    # seconds. Large replies time out before acknowledging any deletions.
    batch_limit = min(body.limit, 10)
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
            body.owner, body.older_than_hours, batch_limit,
        )
        if len(rows) < batch_limit:
            from ..retention_media import claim_expired_media
            rows = list(rows) + list(await claim_expired_media(conn, body.owner, batch_limit-len(rows)))
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
            from ..retention_media import acknowledge_expired_media
            retired = await acknowledge_expired_media(conn, body.owner, keys)
    return {"acknowledged": int(result.rsplit(" ", 1)[-1]), "object_keys": len(keys), "retention_reclaimed": retired}


@router.post("/internal/telegram/observed")
async def observed_events(body: ObserveInput, request: Request):
    authorized(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            source = await source_with_lease(conn, body.chat_id, body.owner, body.claim_request_id)
            rows = await telegram_reconciliation.claim_events(
                conn, source['id'], body.owner, body.limit, body.claim_request_id)
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
    if path not in {"/status", "/dialogs", "/admins", "/members", "/source-info"}:
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
    actor = await message_read.reader(request, admin=True)
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
        "SELECT m.id,m.raw_text,m.media_objects,m.media_state,m.posted_at,m.edited_at,m.revision,m.deleted_at,m.raw_content_expired_at,"
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
    items = [dict(row) | message_read.message_projection(
        dict(row), admin=True, request=request, actor=actor) for row in page]
    return JSONResponse(jsonable_encoder({"items": items, "next_cursor": next_cursor}),
                        headers=message_read.HEADERS)


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
