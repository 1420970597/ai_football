"""Retire expired Telegram objects using the existing source-owner collector."""


async def claim_expired_media(conn, owner, limit):
    if not 1 <= limit <= 500:
        raise ValueError('media_retention_batch_budget')
    async with conn.transaction():
        return await conn.fetch(
            "WITH live_refs AS MATERIALIZED ("
            "SELECT obj->>'object_key' AS key FROM v2_expert_messages m "
            "CROSS JOIN LATERAL jsonb_array_elements(m.media_objects) obj "
            "WHERE m.raw_content_expired_at IS NULL AND m.deleted_at IS NULL "
            "UNION SELECT obj->>'object_key' FROM v2_telegram_events e "
            "CROSS JOIN LATERAL jsonb_array_elements(e.media_objects) obj "
            "WHERE e.raw_content_expired_at IS NULL AND e.is_current),"
            "candidates AS (SELECT q.object_key FROM v2_raw_retention_media q "
            "JOIN v2_source_checkpoints cp ON cp.source_id=q.source_id "
            "WHERE cp.collector_owner=$1 AND cp.lease_until>now() "
            "AND (q.claim_until IS NULL OR q.claim_until<now()) "
            "AND NOT EXISTS(SELECT 1 FROM live_refs r WHERE r.key=q.object_key) "
            "ORDER BY q.queued_at,q.object_key LIMIT $2 FOR UPDATE OF q SKIP LOCKED),"
            "claimed AS (UPDATE v2_raw_retention_media q SET claimed_by=$1,"
            "claim_until=now()+interval '90 seconds' FROM candidates c "
            "WHERE q.object_key=c.object_key RETURNING q.source_id,q.object_key,q.queued_at) "
            "SELECT NULL::uuid AS id,s.platform_chat_id AS chat_id,c.object_key,c.queued_at AS updated_at "
            "FROM claimed c JOIN v2_expert_sources s ON s.id=c.source_id", owner, limit)


async def acknowledge_expired_media(conn, owner, keys):
    result = await conn.execute(
        "DELETE FROM v2_raw_retention_media q USING v2_source_checkpoints cp "
        "WHERE cp.source_id=q.source_id AND cp.collector_owner=$1 AND cp.lease_until>now() "
        "AND q.claimed_by=$1 AND q.claim_until>now() AND q.object_key=ANY($2::text[])",
        owner, keys)
    return int(result.rsplit(' ', 1)[-1])
