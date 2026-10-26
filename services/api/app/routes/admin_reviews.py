from __future__ import annotations

from datetime import datetime
import hashlib
from typing import Literal
import uuid

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import Response as BinaryResponse
from pydantic import BaseModel, Field

from .. import database
from ..admin_reviews import context, retry_problem, retry_review, time_window, trace_payload
from ..auth import _limit, require_admin
from ..blobstore import image_mime, read_image
from ..pagination import decode_cursor, encode_cursor

router = APIRouter(prefix='/api/v1/admin/reviews')


class RetryInput(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    idempotency_key: uuid.UUID


def no_store(response):
    response.headers['Cache-Control'] = 'private, no-store'


@router.get('')
async def list_reviews(request: Request, response: Response, expert_id: uuid.UUID | None = None,
                       source_id: uuid.UUID | None = None,
                       state: Literal['all', 'queued', 'running', 'succeeded', 'unresolved', 'retry', 'failed'] = 'all',
                       from_utc: datetime | None = None, to_utc: datetime | None = None,
                       limit: int = Query(default=30, ge=1, le=100),
                       cursor: str | None = Query(default=None, max_length=1000)):
    admin = await require_admin(request)
    no_store(response)
    start, end = time_window(from_utc, to_utc)
    scope = {'kind': 'admin_reviews', 'actor': str(admin['id']), 'expert': str(expert_id),
             'source': str(source_id), 'state': state, 'from': start.isoformat(), 'to': end.isoformat(), 'limit': limit}
    decoded = decode_cursor(request.app.state.settings.session_secret, scope, cursor)
    try:
        cursor_id = uuid.UUID(decoded[1]) if decoded else None
    except ValueError:
        raise HTTPException(400, '审核分页游标无效') from None
    rows = await database.current_pool().fetch(
        "SELECT j.id,j.message_id,j.state,j.attempts,j.attempt_id,j.lease_until,j.last_error,j.created_at,j.updated_at,"
        "m.source_id,m.expert_id,m.revision,m.media_state,m.posted_at,m.deleted_at,left(m.raw_text,240) AS message_preview,"
        "e.display_nickname,s.display_name AS source_name,"
        "(SELECT count(*) FROM v2_expert_signals sg WHERE sg.message_id=m.id) AS accepted_signal_count,"
        "r.original_job_id AS retry_of "
        "FROM v2_analysis_jobs j JOIN v2_expert_messages m ON m.id=j.message_id "
        "JOIN v2_experts e ON e.id=m.expert_id JOIN v2_expert_sources s ON s.id=m.source_id "
        "LEFT JOIN v2_admin_review_retries r ON r.retry_job_id=j.id "
        "WHERE j.created_at >= $1 AND j.created_at < $2 AND ($3::uuid IS NULL OR m.expert_id=$3) "
        "AND ($4::uuid IS NULL OR m.source_id=$4) AND ($5='all' OR j.state=$5) "
        "AND ($6::timestamptz IS NULL OR (j.created_at,j.id)<($6,$7)) "
        "ORDER BY j.created_at DESC,j.id DESC LIMIT $8", start, end, expert_id, source_id, state,
        decoded[0] if decoded else None, cursor_id, limit + 1,
    )
    page = rows[:limit]
    next_cursor = encode_cursor(request.app.state.settings.session_secret, scope, page[-1]['created_at'], str(page[-1]['id'])) if len(rows) > limit else None
    return {'items': [dict(row) for row in page], 'next_cursor': next_cursor,
            'from_utc': start, 'to_utc': end, 'time_basis': 'review_created_at_utc'}


@router.get('/{job_id}')
async def review_detail(job_id: uuid.UUID, request: Request, response: Response):
    await require_admin(request)
    no_store(response)
    async with database.current_pool().acquire() as conn:
        row = await context(conn, job_id)
        if not row:
            raise HTTPException(404, '审核任务不存在')
        message_fields = ('message_id', 'source_id', 'expert_id', 'platform_message_id', 'revision', 'raw_text',
                          'reply_context', 'posted_at', 'edited_at', 'deleted_at', 'media_state', 'content_hash',
                          'source_name', 'display_nickname', 'platform_sender_id')
        message = {key: row[key] for key in message_fields}
        message['media_objects'] = [{key: value for key, value in item.items() if key not in {'object_key', 'bucket'}} |
                                    {'url': f'/api/v1/admin/reviews/{job_id}/media/{index}'}
                                    for index, item in enumerate(row['media_objects'])]
        job = {key: row[key] for key in ('id', 'state', 'attempts', 'attempt_id', 'lease_until', 'available_at',
                                       'heartbeat_at', 'last_error', 'created_at', 'updated_at')}
        original = await conn.fetchrow('SELECT original_job_id,reason,created_at FROM v2_admin_review_retries WHERE retry_job_id=$1', job_id)
        # Links are bounded; the main list can enumerate all retries by message author/time.
        retries = await conn.fetch('SELECT retry_job_id,reason,created_at FROM v2_admin_review_retries WHERE original_job_id=$1 ORDER BY created_at DESC,id DESC LIMIT 30', job_id)
        signals = await conn.fetch(
            'SELECT sg.id,sg.fixture_id,sg.market_id,sg.market_name,sg.period,sg.line,sg.selection,sg.recommendation_price,'
            'sg.outcome,sg.unit_net,sg.withdrawn_at,f.home_team,f.away_team,f.competition_name,f.kickoff_at '
            'FROM v2_expert_signals sg JOIN v2_fixtures f ON f.id=sg.fixture_id WHERE sg.message_id=$1 '
            'ORDER BY sg.created_at,sg.id LIMIT 100', row['message_id'],
        )
        problem = retry_problem(row)
    return {'review': job, 'message': message, 'retry_of': dict(original) if original else None,
            'retries': [dict(item) for item in retries], 'signals': [dict(item) for item in signals],
            'retry_allowed': problem is None, 'retry_block_reason': problem,
            'history_policy': 'accepted_signals_are_immutable'}


@router.get('/{job_id}/traces')
async def review_traces(job_id: uuid.UUID, request: Request, response: Response,
                         limit: int = Query(default=30, ge=1, le=100),
                         cursor: str | None = Query(default=None, max_length=1000)):
    admin = await require_admin(request)
    no_store(response)
    scope = {'kind': 'admin_review_traces', 'actor': str(admin['id']), 'job': str(job_id), 'limit': limit}
    decoded = decode_cursor(request.app.state.settings.session_secret, scope, cursor)
    try:
        after = int(decoded[1]) if decoded else 0
        if not 0 <= after <= 2147483647:
            raise ValueError
    except ValueError:
        raise HTTPException(400, '审核轨迹游标无效') from None
    pool = database.current_pool()
    if not await pool.fetchval('SELECT EXISTS(SELECT 1 FROM v2_analysis_jobs WHERE id=$1)', job_id):
        raise HTTPException(404, '审核任务不存在')
    rows = await pool.fetch('SELECT sequence_no,trace_kind,payload,created_at FROM v2_analysis_traces '
                            'WHERE job_id=$1 AND sequence_no>$2 ORDER BY sequence_no LIMIT $3', job_id, after, limit + 1)
    page = rows[:limit]
    return {'items': [{**dict(row), 'payload': trace_payload(row['payload'])} for row in page],
            'next_cursor': encode_cursor(request.app.state.settings.session_secret, scope, page[-1]['created_at'], str(page[-1]['sequence_no'])) if len(rows) > limit else None}


@router.post('/{job_id}/retry', status_code=201)
async def request_retry(job_id: uuid.UUID, body: RetryInput, request: Request, response: Response):
    admin = await require_admin(request)
    no_store(response)
    await _limit(request, 'admin_review_retry', str(admin['id']), maximum=20)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            return await retry_review(conn, admin['id'], job_id, body.reason, body.idempotency_key,
                                      str(getattr(request.state, 'request_id', '')))


@router.get('/{job_id}/media/{index}')
async def review_media(job_id: uuid.UUID, index: int, request: Request):
    await require_admin(request)
    objects = await database.current_pool().fetchval(
        'SELECT m.media_objects FROM v2_analysis_jobs j JOIN v2_expert_messages m ON m.id=j.message_id WHERE j.id=$1', job_id,
    )
    if not objects or index < 0 or index >= len(objects):
        raise HTTPException(404, '审核原图不存在')
    media = objects[index]
    if media.get('mime_type') not in {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}:
        raise HTTPException(415, '图片格式不可展示')
    try:
        content = await read_image(media['object_key'])
        if (image_mime(content) != media['mime_type']
                or media.get('sha256') and hashlib.sha256(content).hexdigest() != media['sha256']):
            raise ValueError
    except Exception:
        raise HTTPException(503, '远端原图暂不可读') from None
    return BinaryResponse(content, media_type=media['mime_type'], headers={'Cache-Control': 'private, no-store'})
