"""Bounded, version-bound old-message claims; no schedule advance before commit."""
from datetime import datetime, timezone
import uuid

from fastapi import HTTPException

from .review_policy import REALTIME_WINDOW_SECONDS


RECONCILIATION_LEASE_SECONDS = 30


def _claim_response(row, claim_id, lease_until):
    result = dict(row)
    result.pop('reconcile_claim_id', None)
    result.pop('reconcile_lease_until', None)
    result['claim_id'] = claim_id
    result['claim_lease_until'] = lease_until
    return result


async def _lease_events(conn, rows, owner, request_id):
    if not rows:
        return []
    claims = {row['id']: uuid.uuid4() for row in rows}
    leased = await conn.fetch(
        "UPDATE v2_telegram_events e SET reconcile_claim_id=claims.claim_id,reconcile_owner=$3,"
        "reconcile_request_id=$4,reconcile_lease_until=now()+$5*interval '1 second',reconcile_applied_at=NULL,"
        "reconcile_applied_sha256=NULL,reconcile_acknowledged_at=NULL "
        "FROM unnest($1::uuid[],$2::uuid[]) AS claims(id,claim_id) WHERE e.id=claims.id "
        "RETURNING e.id,e.reconcile_claim_id,e.reconcile_lease_until",
        list(claims), list(claims.values()), owner, request_id, RECONCILIATION_LEASE_SECONDS,
    )
    leases = {row['id']: row for row in leased}
    if len(leased) != len(rows) or set(leases) != set(claims) or any(
        leases[event_id]['reconcile_claim_id'] != claim_id for event_id, claim_id in claims.items()
    ):
        raise RuntimeError('Telegram reconciliation batch lease is incomplete')
    return [_claim_response(row, claims[row['id']], leases[row['id']]['reconcile_lease_until'])
            for row in rows]


async def claim_events(conn, source_id, owner, limit, request_id):
    recent_limit = min(limit, max(1, limit * 4 // 5))
    trusted_author = (
        "EXISTS (SELECT 1 FROM v2_source_experts se "
        "JOIN v2_experts expert ON expert.id=se.expert_id AND expert.enabled "
        "WHERE se.source_id=e.source_id AND se.enabled "
        "AND expert.platform_sender_id=e.content->>'sender_id')"
    )
    available = (
        "e.is_current AND e.next_check_at<=now() "
        "AND (e.reconcile_lease_until IS NULL OR e.reconcile_lease_until<=now() "
        "OR e.reconcile_acknowledged_at IS NOT NULL) "
        f"AND {trusted_author} "
    )
    replay = await conn.fetch(
        "SELECT e.id,e.platform_message_id,e.version_key,e.content,e.reconcile_claim_id,e.reconcile_lease_until "
        "FROM v2_telegram_events e WHERE e.source_id=$1 AND e.reconcile_owner=$2 "
        "AND e.reconcile_request_id=$3 AND e.is_current AND e.reconcile_claim_id IS NOT NULL "
        "AND e.reconcile_lease_until>now() AND e.reconcile_acknowledged_at IS NULL "
        f"AND {trusted_author} "
        "ORDER BY CASE WHEN e.source_version_at>=now()-$4*interval '1 second' THEN 0 ELSE 1 END,"
        "e.source_version_at DESC,e.next_check_at,e.id LIMIT $5",
        source_id, owner, request_id, REALTIME_WINDOW_SECONDS, limit,
    )
    if replay:
        return [_claim_response(row, row['reconcile_claim_id'], row['reconcile_lease_until'])
                for row in replay]
    # One statement gives all buckets the same snapshot. Late arrivals remain
    # due for the next claim; filling a batch must not chase concurrent writes.
    rows = await conn.fetch(
        "WITH recent AS MATERIALIZED ("
        "SELECT e.id,e.platform_message_id,e.version_key,e.content,e.source_version_at,e.next_check_at "
        "FROM v2_telegram_events e "
        f"WHERE e.source_id=$1 AND {available} "
        "AND e.source_version_at>=now()-$4*interval '1 second' "
        "ORDER BY e.source_version_at DESC,e.next_check_at,e.id "
        "LIMIT $2 FOR UPDATE OF e SKIP LOCKED),"
        "historical AS MATERIALIZED ("
        "SELECT e.id,e.platform_message_id,e.version_key,e.content,e.source_version_at,e.next_check_at "
        "FROM v2_telegram_events e "
        f"WHERE e.source_id=$1 AND {available} "
        "AND e.source_version_at<now()-$4*interval '1 second' "
        "ORDER BY e.next_check_at,e.id "
        "LIMIT ($3-(SELECT count(*) FROM recent)) FOR UPDATE OF e SKIP LOCKED),"
        "extra_recent AS MATERIALIZED ("
        "SELECT e.id,e.platform_message_id,e.version_key,e.content,e.source_version_at,e.next_check_at "
        "FROM v2_telegram_events e "
        f"WHERE e.source_id=$1 AND {available} "
        "AND e.source_version_at>=now()-$4*interval '1 second' "
        "AND NOT EXISTS(SELECT 1 FROM recent selected WHERE selected.id=e.id) "
        "ORDER BY e.source_version_at DESC,e.next_check_at,e.id "
        "LIMIT ($3-(SELECT count(*) FROM recent)-(SELECT count(*) FROM historical)) "
        "FOR UPDATE OF e SKIP LOCKED) "
        "SELECT id,platform_message_id,version_key,content FROM ("
        "SELECT recent.*,0 AS bucket FROM recent UNION ALL "
        "SELECT extra_recent.*,0 AS bucket FROM extra_recent UNION ALL "
        "SELECT historical.*,1 AS bucket FROM historical) candidates "
        "ORDER BY bucket,CASE WHEN bucket=0 THEN source_version_at END DESC,next_check_at,id",
        source_id, recent_limit, limit, REALTIME_WINDOW_SECONDS,
    )
    return await _lease_events(conn, rows, owner, request_id)


async def _locked_claim(conn, source_id, owner, event_id, version_key, claim_id):
    row = await conn.fetchrow('SELECT * FROM v2_telegram_events WHERE id=$1 AND source_id=$2 FOR UPDATE',
                              event_id, source_id)
    if (not row or row['version_key'] != version_key or row['reconcile_claim_id'] != claim_id
            or row['reconcile_owner'] != owner):
        raise HTTPException(409, 'Telegram reconciliation claim has changed')
    return row


async def apply_claim(conn, source_id, owner, claim, *, platform_message_id, member_message_ids, event_kind, payload_sha256):
    """Run within the event transaction; a failure rolls this marker back too."""
    row = await _locked_claim(conn, source_id, owner, claim.event_id, claim.version_key, claim.claim_id)
    if row['reconcile_applied_at'] is not None:
        if row['reconcile_applied_sha256'] != payload_sha256:
            raise HTTPException(409, 'Telegram reconciliation retry has conflicting content')
        return True
    if not row['is_current'] or row['reconcile_lease_until'] <= datetime.now(timezone.utc):
        raise HTTPException(409, 'Telegram reconciliation claim is no longer active')
    members = set(row['content']['member_message_ids'])
    requested = set(member_message_ids)
    if ((event_kind == 'delete' and not requested.issubset(members))
            or (event_kind != 'delete' and (platform_message_id != row['platform_message_id'] or requested != members))):
        raise HTTPException(409, 'Telegram reconciliation event scope has changed')
    await conn.execute('UPDATE v2_telegram_events SET reconcile_applied_at=now(),reconcile_applied_sha256=$2 WHERE id=$1',
                       row['id'], payload_sha256)
    return False


async def acknowledge_claim(conn, source_id, owner, claim):
    row = await _locked_claim(conn, source_id, owner, claim.event_id, claim.version_key, claim.claim_id)
    if row['reconcile_acknowledged_at'] is not None:
        return {'acknowledged': True, 'duplicate': True}
    if row['reconcile_applied_at'] is None or row['reconcile_lease_until'] <= datetime.now(timezone.utc):
        raise HTTPException(409, 'Telegram reconciliation has not been committed under an active claim')
    await conn.execute(
        "UPDATE v2_telegram_events SET reconcile_acknowledged_at=now(),"
        "reconcile_request_id=NULL,"
        "next_check_at=CASE WHEN is_current THEN CASE WHEN source_version_at>now()-interval '2 days' "
        "THEN now()+interval '5 minutes' ELSE now()+interval '6 hours' END ELSE next_check_at END WHERE id=$1",
        row['id'],
    )
    return {'acknowledged': True, 'duplicate': False}
