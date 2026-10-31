"""Bounded, version-bound old-message claims; no schedule advance before commit."""
from datetime import datetime, timezone
import uuid

from fastapi import HTTPException

from .review_policy import REALTIME_WINDOW_SECONDS


RECONCILIATION_LEASE_SECONDS = 30


async def claim_events(conn, source_id, owner, limit):
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
    recent = await conn.fetch(
        "SELECT e.id,e.platform_message_id,e.version_key,e.content FROM v2_telegram_events e "
        f"WHERE e.source_id=$1 AND {available} "
        "AND e.source_version_at>=now()-$3*interval '1 second' "
        "ORDER BY e.source_version_at DESC,e.next_check_at,e.id "
        "LIMIT $2 FOR UPDATE OF e SKIP LOCKED",
        source_id, recent_limit, REALTIME_WINDOW_SECONDS,
    )
    historical_limit = limit - len(recent)
    historical = []
    if historical_limit:
        historical = await conn.fetch(
            "SELECT e.id,e.platform_message_id,e.version_key,e.content FROM v2_telegram_events e "
            f"WHERE e.source_id=$1 AND {available} "
            "AND e.source_version_at<now()-$3*interval '1 second' "
            "ORDER BY e.next_check_at,e.id "
            "LIMIT $2 FOR UPDATE OF e SKIP LOCKED",
            source_id, historical_limit, REALTIME_WINDOW_SECONDS,
        )
    remainder = limit - len(recent) - len(historical)
    if remainder:
        claimed_ids = [row['id'] for row in [*recent, *historical]]
        recent.extend(await conn.fetch(
            "SELECT e.id,e.platform_message_id,e.version_key,e.content FROM v2_telegram_events e "
            f"WHERE e.source_id=$1 AND {available} "
            "AND e.source_version_at>=now()-$3*interval '1 second' AND NOT(e.id=ANY($4::uuid[])) "
            "ORDER BY e.source_version_at DESC,e.next_check_at,e.id "
            "LIMIT $2 FOR UPDATE OF e SKIP LOCKED",
            source_id, remainder, REALTIME_WINDOW_SECONDS, claimed_ids,
        ))
    remainder = limit - len(recent) - len(historical)
    if remainder:
        claimed_ids = [row['id'] for row in [*recent, *historical]]
        historical.extend(await conn.fetch(
            "SELECT e.id,e.platform_message_id,e.version_key,e.content FROM v2_telegram_events e "
            f"WHERE e.source_id=$1 AND {available} "
            "AND e.source_version_at<now()-$3*interval '1 second' AND NOT(e.id=ANY($4::uuid[])) "
            "ORDER BY e.next_check_at,e.id "
            "LIMIT $2 FOR UPDATE OF e SKIP LOCKED",
            source_id, remainder, REALTIME_WINDOW_SECONDS, claimed_ids,
        ))
    rows = [*recent, *historical]
    items = []
    for row in rows:
        claim_id = uuid.uuid4()
        lease_until = await conn.fetchval(
            "UPDATE v2_telegram_events SET reconcile_claim_id=$2,reconcile_owner=$3,"
            "reconcile_lease_until=now()+$4*interval '1 second',reconcile_applied_at=NULL,reconcile_applied_sha256=NULL,"
            "reconcile_acknowledged_at=NULL WHERE id=$1 RETURNING reconcile_lease_until",
            row['id'], claim_id, owner, RECONCILIATION_LEASE_SECONDS,
        )
        items.append(dict(row) | {'claim_id': claim_id, 'claim_lease_until': lease_until})
    return items


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
        "next_check_at=CASE WHEN is_current THEN CASE WHEN source_version_at>now()-interval '2 days' "
        "THEN now()+interval '5 minutes' ELSE now()+interval '6 hours' END ELSE next_check_at END WHERE id=$1",
        row['id'],
    )
    return {'acknowledged': True, 'duplicate': False}
