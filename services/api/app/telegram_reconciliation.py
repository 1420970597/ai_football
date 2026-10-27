"""Bounded, version-bound old-message claims; no schedule advance before commit."""
from datetime import datetime, timezone
import uuid

from fastapi import HTTPException


RECONCILIATION_LEASE_SECONDS = 30


async def claim_events(conn, source_id, owner, limit):
    rows = await conn.fetch(
        "SELECT id,platform_message_id,version_key,content FROM v2_telegram_events "
        "WHERE source_id=$1 AND is_current AND next_check_at<=now() "
        "AND (reconcile_lease_until IS NULL OR reconcile_lease_until<=now() "
        "OR reconcile_acknowledged_at IS NOT NULL) "
        "ORDER BY next_check_at,id LIMIT $2 FOR UPDATE SKIP LOCKED", source_id, limit,
    )
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
