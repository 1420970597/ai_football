"""Bounded, read-only admin diagnostics and accounting access."""

import asyncio
from datetime import datetime, timezone
import os
import uuid

from fastapi import APIRouter, HTTPException, Query, Request

from .. import database
from ..auth import require_admin
from ..pagination import decode_cursor, encode_cursor


router = APIRouter(prefix='/api/v1/admin')
COUNT_CAP = 5000


def count_state(value):
    return {'count': min(value or 0, COUNT_CAP), 'capped': bool(value and value > COUNT_CAP)}


@router.get('/diagnostics')
async def diagnostics(request: Request):
    await require_admin(request)
    try:
        async with asyncio.timeout(5):
            async with database.current_pool().acquire() as conn:
                row = await conn.fetchrow(
                    "WITH queue AS (SELECT created_at FROM v2_analysis_jobs WHERE state IN('queued','retry','running') ORDER BY created_at LIMIT 5001),"
                    "media AS (SELECT created_at FROM v2_expert_messages WHERE media_state<>'ready' AND deleted_at IS NULL ORDER BY created_at LIMIT 5001),"
                    "unknowns AS (SELECT created_at FROM v2_bet_orders WHERE state='unknown' ORDER BY created_at LIMIT 5001),"
                    "unresolved AS (SELECT id FROM v2_analysis_jobs WHERE state IN('failed','unresolved') LIMIT 5001),"
                    "stale AS (SELECT id FROM v2_current_quotes WHERE status='active' AND (source_updated_at IS NULL OR "
                    "source_updated_at>now() OR source_updated_at<now()-($1::int*interval '1 second')) LIMIT 5001) "
                    "SELECT (SELECT count(*) FROM queue) AS queue_count,(SELECT min(created_at) FROM queue) AS queue_oldest,"
                    "(SELECT count(*) FROM media) AS media_count,(SELECT min(created_at) FROM media) AS media_oldest,"
                    "(SELECT count(*) FROM unknowns) AS unknown_count,(SELECT min(created_at) FROM unknowns) AS unknown_oldest,"
                    "(SELECT count(*) FROM unresolved) AS unresolved_count,(SELECT count(*) FROM stale) AS stale_count,"
                    "(SELECT source_updated_at FROM v2_fixtures WHERE phase IN('live','scheduled') ORDER BY source_updated_at DESC NULLS LAST LIMIT 1) AS fixture_source_at,"
                    "(SELECT source_updated_at FROM v2_current_quotes WHERE status='active' ORDER BY source_updated_at DESC NULLS LAST LIMIT 1) AS quote_source_at,"
                    "(SELECT created_at FROM v2_expert_messages ORDER BY created_at DESC LIMIT 1) AS message_received_at",
                    request.app.state.settings.quote_max_age_seconds,
                )
                try:
                    await database.check_schema(conn)
                    schema = 'verified'
                except database.MigrationRequired:
                    schema = 'migration_required'
                stats = await conn.fetchrow('SELECT numbackends,blks_read,blks_hit,temp_bytes,deadlocks FROM pg_stat_database WHERE datname=current_database()')
            gateway = await request.app.state.hermes_probe.check()
    except TimeoutError:
        raise HTTPException(503, '运维查询暂时超时，请稍后重试') from None
    configured = all(os.environ.get(key) for key in ('S3_ENDPOINT_URL','S3_BUCKET','S3_ACCESS_KEY','S3_SECRET_KEY'))
    return {
        'checked_at': datetime.now(timezone.utc), 'database': 'reachable', 'schema': schema,
        'hermes': {'gateway': gateway, 'provider': 'unverified'},
        'storage': 'configured_unverified' if configured else 'unconfigured',
        'review_queue': count_state(row['queue_count']) | {'oldest_at': row['queue_oldest']},
        'pending_media': count_state(row['media_count']) | {'oldest_at': row['media_oldest']},
        'unknown_orders': count_state(row['unknown_count']) | {'oldest_at': row['unknown_oldest']},
        'unresolved_reviews': count_state(row['unresolved_count']), 'stale_quotes': count_state(row['stale_count']),
        'freshness': {key: row[key] for key in ('fixture_source_at','quote_source_at','message_received_at')},
        'postgres_counters': dict(stats) if stats else {},
        'limits': 'Counts are bounded; source timestamps are passive evidence, not live upstream probes. Failed migration transactions leave no success record.',
    }


@router.get('/orders')
async def admin_orders(request: Request, state: str | None = Query(default=None, pattern='^(pending|submitting|accepted|rejected|unknown|settled|cancelled)$'),
                       user_id: uuid.UUID | None = None, cursor: str | None = Query(default=None, max_length=1000),
                       limit: int = Query(default=30, ge=1, le=100)):
    actor = await require_admin(request)
    scope = {'kind':'admin_orders', 'actor':str(actor['id']), 'state':state, 'user_id':str(user_id) if user_id else None}
    key = request.app.state.settings.session_secret
    boundary = decode_cursor(key, scope, cursor)
    rows = await database.current_pool().fetch(
        "SELECT o.id,o.user_id,u.email,u.display_name,a.account_label,o.signal_id,o.state,o.stake,o.price,o.reserved_stake,"
        "o.provider_order_no,o.submitted_at,o.settled_net,o.settled_at,o.created_at,o.updated_at,"
        "s.fixture_id,s.market_name,s.period,s.line,s.selection,f.home_team,f.away_team "
        "FROM v2_bet_orders o JOIN v2_users u ON u.id=o.user_id LEFT JOIN v2_leyu_accounts a ON a.id=o.account_id "
        "JOIN v2_expert_signals s ON s.id=o.signal_id JOIN v2_fixtures f ON f.id=s.fixture_id "
        "WHERE ($1::text IS NULL OR o.state=$1) AND ($2::uuid IS NULL OR o.user_id=$2) "
        "AND ($3::timestamptz IS NULL OR (o.created_at,o.id)<($3,$4::uuid)) ORDER BY o.created_at DESC,o.id DESC LIMIT $5",
        state, user_id, boundary[0] if boundary else None, uuid.UUID(boundary[1]) if boundary else None, limit+1,
    )
    items = [dict(row) for row in rows[:limit]]
    next_cursor = encode_cursor(key, scope, items[-1]['created_at'], str(items[-1]['id'])) if len(rows)>limit and items else None
    return {'items':items, 'next_cursor':next_cursor, 'read_only':True,
            'profit_basis':'provider-confirmed actual settlement; separate from expert unit return'}
