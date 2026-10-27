"""Administrator review inspection and immutable, idempotent manual retry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from fastapi import HTTPException

from .auth import audit
from .review_state import trace

STATES = {'queued', 'running', 'succeeded', 'unresolved', 'retry', 'failed'}
SECRET_FIELDS = {'authorization', 'api_key', 'access_token', 'refresh_token', 'token', 'password',
                 'secret', 'api_hash', 'session_string', 'cookie', 'set-cookie', 'object_key', 'bucket'}


def trace_payload(value):
    """Keep model/tool evidence while excluding credentials and inline image bytes."""
    if isinstance(value, dict):
        return {key: '[已隐藏]' if str(key).lower() in SECRET_FIELDS else trace_payload(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [trace_payload(item) for item in value]
    if isinstance(value, str) and value.lower().startswith('data:image/'):
        return '[图片编码已省略，请查看消息原图]'
    return value


def time_window(start: datetime | None, end: datetime | None):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = start if start is not None else today - timedelta(days=30)
    end = end if end is not None else today + timedelta(days=1)
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, '查询时间必须包含时区')
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if end <= start or end - start > timedelta(days=366):
        raise HTTPException(422, '时间范围须大于零且不超过366天')
    return start, end


async def context(conn, job_id):
    return await conn.fetchrow(
        "SELECT j.*,m.source_id,m.expert_id,m.platform_message_id,m.revision,m.deleted_at,m.media_state,"
        "m.raw_text,m.media_objects,m.reply_context,m.posted_at,m.edited_at,m.content_hash,"
        "s.display_name AS source_name,s.enabled AS source_enabled,s.allow_media,"
        "e.display_nickname,e.platform_sender_id,e.enabled AS expert_enabled,se.enabled AS trusted,"
        "NOT EXISTS(SELECT 1 FROM v2_expert_messages newer WHERE newer.source_id=m.source_id "
        "AND newer.platform_message_id=m.platform_message_id AND newer.revision>m.revision) AS is_latest,"
        "EXISTS(SELECT 1 FROM v2_expert_signals sg JOIN v2_expert_messages sm ON sm.id=sg.message_id "
        "WHERE sm.source_id=m.source_id AND sm.platform_message_id=m.platform_message_id) AS has_accepted_signals,"
        "EXISTS(SELECT 1 FROM v2_analysis_jobs aj JOIN v2_expert_messages am ON am.id=aj.message_id "
        "WHERE am.source_id=m.source_id AND am.platform_message_id=m.platform_message_id "
        "AND aj.state IN ('queued','running','retry')) AS has_active_job,"
        "EXISTS(SELECT 1 FROM v2_telegram_events ev WHERE ev.message_id=m.id) AS has_event,"
        "EXISTS(SELECT 1 FROM v2_telegram_events ev WHERE ev.message_id=m.id AND ev.is_current "
        "AND ev.event_kind<>'delete' AND ev.media_state='ready') AS event_current "
        "FROM v2_analysis_jobs j JOIN v2_expert_messages m ON m.id=j.message_id "
        "JOIN v2_expert_sources s ON s.id=m.source_id JOIN v2_experts e ON e.id=m.expert_id "
        "LEFT JOIN v2_source_experts se ON se.source_id=m.source_id AND se.expert_id=m.expert_id "
        "WHERE j.id=$1", job_id,
    )


def retry_problem(row):
    if row['has_accepted_signals']:
        return 'accepted_signals_are_immutable'
    if row['state'] not in {'failed', 'unresolved'}:
        return 'only_failed_or_unresolved_can_retry'
    if row['deleted_at'] or not row['is_latest'] or row['has_event'] and not row['event_current']:
        return 'message_deleted_or_superseded'
    if not all((row['source_enabled'], row['expert_enabled'], row['trusted'])):
        return 'source_or_author_not_trusted'
    if row['media_state'] != 'ready' or row['media_objects'] and not row['allow_media']:
        return 'media_not_ready_or_disabled'
    if not row['raw_text'].strip() and not row['media_objects']:
        return 'message_content_missing'
    if row['has_active_job']:
        return 'another_review_is_active'
    return None


async def retry_review(conn, actor_id, job_id, reason, idempotency_key, request_id=''):
    reason = reason.strip()
    if len(reason) < 3:
        raise HTTPException(422, '重试原因至少包含三个非空字符')
    await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1))', f'admin-review:{actor_id}:{idempotency_key}')
    previous = await conn.fetchrow(
        'SELECT r.*,j.state FROM v2_admin_review_retries r JOIN v2_analysis_jobs j ON j.id=r.retry_job_id '
        'WHERE r.actor_id=$1 AND r.idempotency_key=$2', actor_id, idempotency_key,
    )
    if previous:
        if previous['original_job_id'] != job_id or previous['reason'] != reason:
            raise HTTPException(409, '幂等标识已用于不同重试请求')
        return {'id': str(previous['retry_job_id']), 'original_review_id': str(job_id),
                'state': previous['state'], 'replayed': True}
    row = await context(conn, job_id)
    if not row:
        raise HTTPException(404, '审核任务不存在')
    # Match the collector's logical-message lock so an edit cannot slip between
    # checking the latest revision and enqueueing its replacement review.
    await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1))', f"telegram:{row['source_id']}:{row['platform_message_id']}")
    await conn.fetchrow('SELECT id FROM v2_analysis_jobs WHERE id=$1 FOR UPDATE', job_id)
    await conn.fetchrow('SELECT id FROM v2_expert_messages WHERE id=$1 FOR SHARE', row['message_id'])
    await conn.fetchrow('SELECT id FROM v2_expert_sources WHERE id=$1 FOR SHARE', row['source_id'])
    await conn.fetchrow('SELECT id FROM v2_experts WHERE id=$1 FOR SHARE', row['expert_id'])
    await conn.fetchrow('SELECT expert_id FROM v2_source_experts WHERE source_id=$1 AND expert_id=$2 FOR SHARE', row['source_id'], row['expert_id'])
    row = await context(conn, job_id)
    problem = retry_problem(row)
    if problem:
        raise HTTPException(409, detail={'code': problem, 'message': '当前证据不允许重新生成推荐'})
    new_id, record_id = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key) VALUES($1,$2,'queued',$3)",
        new_id, row['message_id'], f'admin-review:{actor_id}:{idempotency_key}',
    )
    await conn.execute(
        'INSERT INTO v2_admin_review_retries(id,actor_id,idempotency_key,original_job_id,retry_job_id,reason) VALUES($1,$2,$3,$4,$5,$6)',
        record_id, actor_id, idempotency_key, job_id, new_id, reason,
    )
    payload = {'actor_id': str(actor_id), 'reason': reason, 'original_review_id': str(job_id),
               'retry_review_id': str(new_id), 'message_revision': row['revision']}
    await trace(conn, job_id, 'admin_retry_requested', payload)
    await trace(conn, new_id, 'admin_retry_created', payload)
    await audit(conn, actor_id, 'admin', 'review_retry_requested', 'analysis_job', str(job_id), request_id, payload)
    return {'id': str(new_id), 'original_review_id': str(job_id), 'state': 'queued', 'replayed': False}
