from __future__ import annotations

import uuid
import hashlib
import hmac
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

from .. import database
from ..blobstore import image_mime, read_image
from ..expert_statistics import StatisticsScope, statistics_scope, summaries, trend
from .. import message_read
from .. import media_grants
from .public import _decode_cursor, _encode_cursor, _has_entitlement

router = APIRouter(prefix='/api/v1/public')


@router.get('/leagues')
async def leagues(match_date: date | None = Query(default=None, alias='date'),
                  phase: str = Query(default='all', pattern='^(all|live|scheduled|finished|cancelled)$'),
                  sport: str = Query(default='football', max_length=20), tz: str = Query(default='Asia/Shanghai', max_length=80)):
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        raise HTTPException(422, '未知时区') from None
    start = datetime.combine(match_date, time.min, zone).astimezone(timezone.utc) if match_date else None
    end = datetime.combine(match_date + timedelta(days=1), time.min, zone).astimezone(timezone.utc) if match_date else None
    rows = await database.current_pool().fetch(
        "SELECT competition_id,competition_name,COUNT(*) AS match_count FROM v2_fixtures WHERE sport=$1 "
        "AND ($2='all' OR phase=$2) AND ($3::timestamptz IS NULL OR kickoff_at >= $3) "
        "AND ($4::timestamptz IS NULL OR kickoff_at < $4) "
        "AND ($3::timestamptz IS NOT NULL OR phase IN ('live','scheduled')) "
        "GROUP BY competition_id,competition_name ORDER BY competition_name,competition_id", sport, phase, start, end,
    )
    return {'items': [dict(row) for row in rows]}


@router.get('/experts/{expert_id}')
async def expert_detail(expert_id: uuid.UUID, scope: StatisticsScope = Depends(statistics_scope)):
    rows = await summaries(database.current_pool(), scope.start, scope.end, scope=scope, expert_id=expert_id)
    if not rows:
        raise HTTPException(404, '专家不存在')
    return {'expert': rows[0], **scope.response()}


@router.get('/experts/{expert_id}/trend')
async def expert_trend(expert_id: uuid.UUID, scope: StatisticsScope = Depends(statistics_scope)):
    pool = database.current_pool()
    summary = await summaries(pool, scope.start, scope.end, scope=scope, expert_id=expert_id)
    if not summary:
        raise HTTPException(404, '专家不存在')
    return {'items': await trend(pool, expert_id, scope), 'summary': summary[0], **scope.response(),
            'timezone': 'UTC', 'basis': 'latest_settlement_at_latest_result'}


async def require_history(request: Request):
    return await message_read.reader(request)


def media_links(message: dict, request, actor):
    message['media_objects'] = message_read.message_projection(message, request=request, actor=actor)['media_objects']
    return message


@router.get('/experts/{expert_id}/messages')
async def expert_messages(expert_id: uuid.UUID, request: Request, cursor: str | None = Query(default=None, max_length=1000),
                          limit: int = Query(default=30, ge=1, le=100)):
    actor = await require_history(request)
    cursor_scope = {'kind': 'expert_messages', 'expert_id': str(expert_id)}
    decoded = _decode_cursor(cursor, request, cursor_scope)
    try:
        cursor_id = uuid.UUID(decoded[1]) if decoded else None
    except ValueError:
        raise HTTPException(400, '消息游标无效') from None
    rows = await database.current_pool().fetch(
        "SELECT m.id,m.raw_text,m.media_objects,m.media_state,m.posted_at,m.edited_at,m.revision,m.deleted_at,"
        "e.display_nickname,j.state AS analysis_state,j.last_error AS analysis_error "
        "FROM v2_expert_messages m JOIN v2_experts e ON e.id=m.expert_id "
        "LEFT JOIN LATERAL(SELECT state,last_error FROM v2_analysis_jobs WHERE message_id=m.id ORDER BY created_at DESC LIMIT 1) j ON TRUE "
        "WHERE m.expert_id=$1 AND ($2::timestamptz IS NULL OR (m.posted_at,m.id)<($2,$3)) "
        "ORDER BY m.posted_at DESC,m.id DESC LIMIT $4", expert_id, decoded[0] if decoded else None, cursor_id, limit+1,
    )
    page = rows[:limit]
    return {'items': [media_links(dict(row), request, actor) for row in page],
            'next_cursor': _encode_cursor(page[-1]['posted_at'], str(page[-1]['id']), request, cursor_scope) if len(rows)>limit and page else None}


@router.get('/messages/{message_id}')
async def message_detail(message_id: uuid.UUID, request: Request):
    actor = await require_history(request)
    pool = database.current_pool()
    message = await pool.fetchrow(
        'SELECT m.*,e.display_nickname,s.display_name AS source_name FROM v2_expert_messages m '
        'JOIN v2_experts e ON e.id=m.expert_id JOIN v2_expert_sources s ON s.id=m.source_id WHERE m.id=$1', message_id,
    )
    if not message:
        raise message_read.private_error(404, '消息原文不存在')
    return JSONResponse(jsonable_encoder(await message_read.detail(pool, request, actor, message)), headers=message_read.HEADERS)


@router.get('/messages/{message_id}/traces')
async def message_traces(message_id: uuid.UUID, request: Request,
                         limit: int = Query(default=30, ge=1, le=100),
                         cursor: str | None = Query(default=None, max_length=1000)):
    actor = await require_history(request)
    pool = database.current_pool()
    if not await pool.fetchval('SELECT EXISTS(SELECT 1 FROM v2_expert_messages WHERE id=$1)', message_id):
        raise message_read.private_error(404, '消息原文不存在')
    return JSONResponse(jsonable_encoder(await message_read.trace_page(pool, request, actor, message_id, limit=limit, cursor=cursor)),
                        headers=message_read.HEADERS)


@router.get('/messages/{message_id}/signals')
async def message_signals(message_id: uuid.UUID, request: Request,
                          limit: int = Query(default=30, ge=1, le=100),
                          cursor: str | None = Query(default=None, max_length=1000)):
    actor = await require_history(request)
    pool = database.current_pool()
    if not await pool.fetchval('SELECT EXISTS(SELECT 1 FROM v2_expert_messages WHERE id=$1)', message_id):
        raise message_read.private_error(404, '消息原文不存在')
    return JSONResponse(jsonable_encoder(await message_read.signal_page(pool, request, actor, message_id, limit=limit, cursor=cursor)),
                        headers=message_read.HEADERS)


@router.get('/messages/{message_id}/tools/{trace_id}')
async def message_tool_response(message_id: uuid.UUID, trace_id: uuid.UUID, request: Request):
    await require_history(request)
    return JSONResponse(jsonable_encoder(await message_read.tool_response(database.current_pool(), message_id, trace_id)),
                        headers=message_read.HEADERS)


@router.get('/messages/{message_id}/media/{index}')
async def message_media(message_id: uuid.UUID, index: int, request: Request):
    actor = await require_history(request)
    return await read_message_media(message_id, index, request=request, actor=actor)


async def read_message_media(message_id: uuid.UUID, index: int, *, request: Request, actor, context='user'):
    row = await database.current_pool().fetchrow('SELECT id,revision,media_objects FROM v2_expert_messages WHERE id=$1', message_id)
    objects = row['media_objects'] if row else None
    if not objects or index < 0 or index >= len(objects):
        raise message_read.private_error(404, '消息图片不存在')
    media = objects[index]
    if not isinstance(media, dict):
        raise message_read.private_error(415, '图片存储证据无效')
    media_grants.verify(request.app.state.settings.session_secret, actor, dict(row), index, media, context,
                        request.query_params.get('grant'))
    mime = media.get('mime_type')
    if mime not in {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}:
        raise message_read.private_error(415, '图片格式不可展示')
    try:
        content = await read_image(media['object_key'])
        if image_mime(content) != mime:
            raise ValueError('media type mismatch')
        if media.get('sha256') and not hmac.compare_digest(hashlib.sha256(content).hexdigest(), str(media['sha256'])):
            raise ValueError('media digest mismatch')
        if media.get('bytes') is not None and len(content) != media['bytes']:
            raise ValueError('media length mismatch')
    except Exception:
        raise message_read.private_error(503, '远端图片暂不可读') from None
    return Response(content, media_type=mime, headers={'Cache-Control': 'private, no-store', 'Content-Disposition': 'inline'})
