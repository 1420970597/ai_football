"""Private message evidence readers; bounded pages never alter stored originals."""
from __future__ import annotations

import time
import uuid

from fastapi import HTTPException

from .admin_reviews import trace_payload
from .auth import get_identity, require_admin
from .membership import has_entitlement
from .pagination import decode_cursor, encode_cursor
from .review_tool_evidence import MAX_CHUNKS, replay_snapshot, snapshot_result
from . import media_grants

HEADERS = {'Cache-Control': 'private, no-store'}
CURSOR_SECONDS = 900
TOOL_KINDS = ('mcp_search_matches', 'mcp_resolve_match', 'mcp_list_match_markets', 'mcp_record_expert_signals')


def private_error(code, detail):
    return HTTPException(code, detail, headers=HEADERS)


async def reader(request, *, admin=False):
    try:
        actor = await require_admin(request) if admin else await get_identity(request)
        if actor is None or actor['role'] != 'admin' and not await has_entitlement(actor['id'], 'expert_full_history'):
            raise private_error(403, '完整专家记录需要会员权益')
        return actor
    except HTTPException as error:
        error.headers = {**(error.headers or {}), **HEADERS}
        raise


def message_projection(row, *, admin=False, request=None, actor=None):
    allowed = ('id', 'raw_text', 'posted_at', 'edited_at', 'revision', 'deleted_at', 'media_state',
               'reply_context', 'display_nickname', 'source_name', 'created_at', 'last_error', 'source_evidence')
    result = {key: trace_payload(row[key]) for key in allowed if key in row}
    root = '/api/v1/admin/telegram/messages/' if admin else '/api/v1/public/messages/'
    result['media_objects'] = []
    for index, media in enumerate(row.get('media_objects') or []):
        if not isinstance(media, dict):
            continue
        item = {key: media[key] for key in ('mime_type', 'bytes', 'sha256', 'width', 'height') if key in media}
        url = root + str(row['id']) + '/media/' + str(index)
        if request is not None and actor is not None:
            grant = media_grants.issue(request.app.state.settings.session_secret, actor, row, index, media,
                                       'admin' if admin else 'user')
            url += '?grant=' + grant
            item['expires_in_seconds'] = media_grants.MEDIA_GRANT_SECONDS
        item['url'] = url
        result['media_objects'].append(item)
    return result


def _scope(actor, message_id, context, stream, limit):
    return {'kind': 'message_evidence_v1', 'message': str(message_id), 'context': context,
            'actor': str(actor['id']) if actor else None, 'session': actor.get('_token_hash') if actor else None,
            'stream': stream, 'limit': limit}


def _decode(request, actor, message_id, context, stream, limit, cursor):
    if not cursor:
        return None
    try:
        stamp, identifier = decode_cursor(request.app.state.settings.session_secret,
            _scope(actor, message_id, context, stream, limit), cursor)
        parts = identifier.split(':')
        issued = int(parts[-1])
        if not 0 <= time.time() - issued <= CURSOR_SECONDS:
            raise ValueError()
        if stream == 'traces':
            job, sequence, trace_id = uuid.UUID(parts[0]), int(parts[1]), uuid.UUID(parts[2])
            if len(parts) != 4 or not 1 <= sequence <= 2147483647:
                raise ValueError()
            return stamp, job, sequence, trace_id, issued
        if len(parts) != 2:
            raise ValueError()
        return stamp, uuid.UUID(parts[0]), issued
    except (HTTPException, IndexError, TypeError, ValueError):
        raise private_error(400, '阅读游标无效、已过期或身份及上下文已变化') from None


def _encode(request, actor, message_id, context, stream, limit, stamp, identity, issued):
    if actor is None:
        raise private_error(403, '完整专家记录需要会员权益')
    return encode_cursor(request.app.state.settings.session_secret,
        _scope(actor, message_id, context, stream, limit), stamp, identity + ':' + str(issued))


def trace_projection(row, message_id, *, context='user'):
    item = dict(row)
    payload = item.get('payload') or {}
    if item['trace_kind'] == 'tool_response_chunk':
        # The SQL already omits encoded chunks, so first render does not fetch
        # or JSON-serialize base64. A separate click replays the full snapshot.
        payload = {key: value for key, value in payload.items() if key != 'data_base64'}
        payload['content_location'] = '通过对应工具记录的“完整工具返回”读取；原始分片保持保存'
        item['payload'] = trace_payload(payload)
    else:
        item['payload'] = trace_payload(payload)
    if item['trace_kind'] in TOOL_KINDS and isinstance(payload.get('response_snapshot'), dict):
        root = '/api/v1/admin/telegram/messages/' if context == 'admin' else '/api/v1/public/messages/'
        item['tool_response_url'] = root + str(message_id) + '/tools/' + str(item['id'])
    item.pop('job_created_at', None)
    return item


async def trace_page(pool, request, actor, message_id, *, context='user', limit=30, cursor=None):
    decoded = _decode(request, actor, message_id, context, 'traces', limit, cursor)
    stamp, job, sequence, trace_id = decoded[:4] if decoded else (None, None, None, None)
    rows = await pool.fetch(
        "SELECT t.id,j.id AS review_id,j.state,j.last_error,j.created_at AS job_created_at,"
        "t.sequence_no,t.trace_kind,CASE WHEN t.trace_kind='tool_response_chunk' THEN t.payload-'data_base64' ELSE t.payload END AS payload,t.created_at "
        "FROM v2_analysis_jobs j JOIN v2_analysis_traces t ON t.job_id=j.id WHERE j.message_id=$1 "
        "AND ($2::timestamptz IS NULL OR (j.created_at,j.id,t.sequence_no,t.id)>($2,$3,$4,$5)) "
        "ORDER BY j.created_at,j.id,t.sequence_no,t.id LIMIT $6", message_id, stamp, job, sequence, trace_id, limit+1)
    page = rows[:limit]
    issued = decoded[4] if decoded else int(time.time())
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1]
        next_cursor = _encode(request, actor, message_id, context, 'traces', limit, last['job_created_at'],
            str(last['review_id'])+':'+str(last['sequence_no'])+':'+str(last['id']), issued)
    return {'items': [trace_projection(row, message_id, context=context) for row in page],
            'next_cursor': next_cursor, 'page_size': limit, 'has_more': bool(next_cursor)}


async def latest_summary(pool, message_id):
    row = await pool.fetchrow(
        "SELECT t.id,j.id AS review_id,j.state,j.last_error,t.sequence_no,t.trace_kind,t.payload,t.created_at "
        "FROM v2_analysis_jobs j JOIN v2_analysis_traces t ON t.job_id=j.id "
        "WHERE j.message_id=$1 AND t.trace_kind='system_review_result' "
        "ORDER BY j.created_at DESC,j.id DESC,t.sequence_no DESC,t.id DESC LIMIT 1", message_id)
    return trace_projection(row, message_id) if row else None


async def latest_model_result(pool, message_id):
    """Show the latest attempt's output without paging through tool chunks.

    Do not substitute an older job or attempt when the current review is still
    running or failed: that would make stale analysis look current.
    """
    row = await pool.fetchrow(
        "SELECT t.id,j.id AS review_id,j.state,j.last_error,t.sequence_no,t.trace_kind,t.payload,t.created_at "
        "FROM (SELECT id,state,last_error,attempt_id FROM v2_analysis_jobs WHERE message_id=$1 "
        "ORDER BY created_at DESC,id DESC LIMIT 1) j JOIN v2_analysis_traces t ON t.job_id=j.id "
        "WHERE t.trace_kind='hermes_result' AND t.payload->>'attempt_id'=j.attempt_id::text "
        "ORDER BY t.sequence_no DESC,t.id DESC LIMIT 1", message_id)
    return trace_projection(row, message_id) if row else None


async def signal_page(pool, request, actor, message_id, *, context='user', limit=30, cursor=None):
    decoded = _decode(request, actor, message_id, context, 'signals', limit, cursor)
    rows = await pool.fetch(
        "SELECT s.*,f.competition_name,f.home_team,f.away_team,f.kickoff_at,f.phase AS fixture_phase,f.match_state,"
        "(SELECT ev.evidence->>'quote_id' FROM v2_quote_evidence ev WHERE ev.reference_id=s.id "
        "AND ev.evidence_kind='expert_signal' ORDER BY ev.captured_at LIMIT 1) AS quote_id "
        "FROM v2_expert_signals s JOIN v2_fixtures f ON f.id=s.fixture_id WHERE s.message_id=$1 "
        "AND ($2::timestamptz IS NULL OR (s.created_at,s.id)>($2,$3)) ORDER BY s.created_at,s.id LIMIT $4",
        message_id, decoded[0] if decoded else None, decoded[1] if decoded else None, limit+1)
    page = rows[:limit]
    issued = decoded[2] if decoded else int(time.time())
    next_cursor = _encode(request, actor, message_id, context, 'signals', limit, page[-1]['created_at'],
        str(page[-1]['id']), issued) if len(rows)>limit and page else None
    return {'items': [dict(row) for row in page], 'next_cursor': next_cursor, 'page_size': limit, 'has_more': bool(next_cursor)}


async def detail(pool, request, actor, message, *, context='user'):
    traces = await trace_page(pool, request, actor, message['id'], context=context)
    signals = await signal_page(pool, request, actor, message['id'], context=context)
    if not traces['items']:
        pending = await pool.fetchrow('SELECT id AS review_id,state,last_error,created_at FROM v2_analysis_jobs '
            'WHERE message_id=$1 ORDER BY created_at DESC,id DESC LIMIT 1', message['id'])
        if pending:
            traces['items'] = [{**dict(pending), 'trace_kind': 'review_state',
                                'payload': {'note': '审核尚未保存轨迹，原文继续保留'}}]
    return {'message': message_projection(dict(message), admin=context=='admin', request=request, actor=actor), 'analysis': traces['items'],
            'analysis_next_cursor': traces['next_cursor'], 'analysis_has_more': traces['has_more'],
            'system_summary': await latest_summary(pool, message['id']),
            'latest_model_result': await latest_model_result(pool, message['id']),
            'signals': signals['items'], 'signals_next_cursor': signals['next_cursor'],
            'signals_has_more': signals['has_more'], 'page_size': 30}


async def tool_response(pool, message_id, trace_id):
    row = await pool.fetchrow(
        "SELECT t.job_id,t.trace_kind,t.payload FROM v2_analysis_traces t JOIN v2_analysis_jobs j ON j.id=t.job_id "
        "WHERE t.id=$1 AND j.message_id=$2 AND t.trace_kind=ANY($3::text[])", trace_id, message_id, list(TOOL_KINDS))
    if not row:
        raise private_error(404, '完整工具记录不存在')
    manifest = (row['payload'] or {}).get('response_snapshot')
    if not isinstance(manifest, dict):
        raise private_error(409, '此历史工具记录没有完整快照，原有摘要仍可阅读')
    chunks = await pool.fetch(
        "SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='tool_response_chunk' "
        "AND payload->>'call_id'=$2 ORDER BY sequence_no LIMIT $3", row['job_id'], str(manifest.get('call_id')), MAX_CHUNKS+1)
    try:
        result = snapshot_result(row['trace_kind'], replay_snapshot(manifest, [chunk['payload'] for chunk in chunks]))
    except (KeyError, TypeError, ValueError, HTTPException):
        raise private_error(409, '完整工具快照校验失败，未返回不完整内容') from None
    return {'trace_id': str(trace_id), 'tool': row['trace_kind'], 'result': trace_payload(result),
            'snapshot_sha256': manifest['sha256'], 'complete': True}
