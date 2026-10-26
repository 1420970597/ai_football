from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from .. import database
from ..blobstore import image_mime, read_image
from ..expert_statistics import DEFINITIONS, METRIC_SQL, metrics
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
async def expert_detail(expert_id: uuid.UUID, days: int = Query(default=30, ge=1, le=3650)):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    row = await database.current_pool().fetchrow(
        'SELECT e.id,e.display_nickname,e.avatar_object_key,e.enabled,' + METRIC_SQL +
        ' FROM v2_experts e LEFT JOIN v2_expert_signals s ON s.expert_id=e.id AND s.created_at >= $2 AND s.created_at < $3 '
        'WHERE e.id=$1 AND e.enabled GROUP BY e.id', expert_id, start, end,
    )
    if not row:
        raise HTTPException(404, '专家不存在')
    return {'expert': metrics(dict(row)), 'from_utc': start, 'to_utc': end, 'definitions': DEFINITIONS}


@router.get('/experts/{expert_id}/trend')
async def expert_trend(expert_id: uuid.UUID, days: int = Query(default=365, ge=1, le=3650)):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    rows = await database.current_pool().fetch(
        "WITH daily AS (SELECT date_trunc('day',settled_at AT TIME ZONE 'UTC') AS day,"
        "SUM(unit_net) AS net,COUNT(*) AS settled,SUM(effective_stake) AS effective "
        "FROM v2_expert_signals WHERE expert_id=$1 AND outcome<>'pending' AND settled_at >= $2 AND settled_at < $3 "
        "GROUP BY 1) SELECT day,net,settled,effective,SUM(net) OVER(ORDER BY day) AS cumulative_unit_net,"
        "SUM(net) OVER(ORDER BY day)/SUM(settled) OVER(ORDER BY day) AS average_unit_net,"
        "SUM(net) OVER(ORDER BY day)/NULLIF(SUM(effective) OVER(ORDER BY day),0) AS unit_roi "
        "FROM daily ORDER BY day", expert_id, start, end,
    )
    return {'items': [dict(row) for row in rows], 'from_utc': start, 'to_utc': end,
            'timezone': 'UTC', 'definitions': DEFINITIONS, 'basis': 'latest_settlement_at_latest_result'}


async def require_history(request: Request):
    if not await _has_entitlement(request, 'expert_full_history'):
        raise HTTPException(403, '完整专家记录需要会员权益')


def media_links(message: dict):
    message['media_objects'] = [
        {k: v for k, v in media.items() if k not in {'object_key', 'bucket'}} |
        {'url': f"/api/v1/public/messages/{message['id']}/media/{index}"}
        for index, media in enumerate(message.get('media_objects') or [])
    ]
    return message


@router.get('/experts/{expert_id}/messages')
async def expert_messages(expert_id: uuid.UUID, request: Request, cursor: str | None = Query(default=None, max_length=1000),
                          limit: int = Query(default=30, ge=1, le=100)):
    await require_history(request)
    decoded = _decode_cursor(cursor)
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
    return {'items': [media_links(dict(row)) for row in page],
            'next_cursor': _encode_cursor(page[-1]['posted_at'], str(page[-1]['id'])) if len(rows)>limit and page else None}


@router.get('/messages/{message_id}')
async def message_detail(message_id: uuid.UUID, request: Request):
    await require_history(request)
    pool = database.current_pool()
    message = await pool.fetchrow(
        'SELECT m.*,e.display_nickname,s.display_name AS source_name FROM v2_expert_messages m '
        'JOIN v2_experts e ON e.id=m.expert_id JOIN v2_expert_sources s ON s.id=m.source_id WHERE m.id=$1', message_id,
    )
    if not message:
        raise HTTPException(404, '消息原文不存在')
    traces = await pool.fetch(
        'SELECT j.id AS review_id,j.state,j.last_error,t.sequence_no,t.trace_kind,t.payload,t.created_at '
        'FROM v2_analysis_jobs j LEFT JOIN v2_analysis_traces t ON t.job_id=j.id '
        'WHERE j.message_id=$1 ORDER BY j.created_at,t.sequence_no', message_id,
    )
    signals = await pool.fetch(
        "SELECT s.*,f.competition_name,f.home_team,f.away_team,f.kickoff_at,"
        "(SELECT ev.evidence->>'quote_id' FROM v2_quote_evidence ev WHERE ev.reference_id=s.id "
        "AND ev.evidence_kind='expert_signal' ORDER BY ev.captured_at LIMIT 1) AS quote_id "
        'FROM v2_expert_signals s '
        'JOIN v2_fixtures f ON f.id=s.fixture_id WHERE s.message_id=$1 ORDER BY s.created_at,s.id', message_id,
    )
    return {'message': media_links(dict(message)), 'analysis': [dict(row) for row in traces],
            'signals': [dict(row) for row in signals]}


@router.get('/messages/{message_id}/media/{index}')
async def message_media(message_id: uuid.UUID, index: int, request: Request):
    await require_history(request)
    objects = await database.current_pool().fetchval('SELECT media_objects FROM v2_expert_messages WHERE id=$1', message_id)
    if not objects or index < 0 or index >= len(objects):
        raise HTTPException(404, '消息图片不存在')
    media = objects[index]
    mime = media.get('mime_type')
    if mime not in {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}:
        raise HTTPException(415, '图片格式不可展示')
    try:
        content = await read_image(media['object_key'])
        if image_mime(content) != mime:
            raise ValueError('media type mismatch')
    except Exception:
        raise HTTPException(503, '远端图片暂不可读') from None
    return Response(content, media_type=mime, headers={'Cache-Control': 'private, no-store', 'Content-Disposition': 'inline'})
