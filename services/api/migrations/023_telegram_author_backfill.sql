ALTER TABLE v2_source_experts ADD COLUMN backfill_since TIMESTAMPTZ;
ALTER TABLE v2_source_experts ADD COLUMN backfill_cursor BIGINT;
ALTER TABLE v2_source_experts ADD COLUMN backfill_until_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE v2_source_experts ADD COLUMN backfill_completed_at TIMESTAMPTZ;

UPDATE v2_source_experts se
SET backfill_since = now() - interval '7 days',
    backfill_until_id = COALESCE((SELECT cp.last_message_id FROM v2_source_checkpoints cp WHERE cp.source_id=se.source_id), 0),
    backfill_cursor = COALESCE((SELECT cp.last_message_id FROM v2_source_checkpoints cp WHERE cp.source_id=se.source_id), 0) + 1,
    backfill_completed_at = CASE WHEN COALESCE((SELECT cp.last_message_id FROM v2_source_checkpoints cp WHERE cp.source_id=se.source_id), 0) = 0 THEN now() ELSE NULL END
FROM v2_experts e
WHERE e.id = se.expert_id
  AND se.enabled AND e.enabled
  AND e.platform_sender_id ~ '^[1-9][0-9]{0,18}$';

UPDATE v2_source_experts SET backfill_cursor=0,backfill_completed_at=COALESCE(backfill_completed_at,now())
WHERE backfill_cursor IS NULL;
ALTER TABLE v2_source_experts ALTER COLUMN backfill_cursor SET DEFAULT 0;
ALTER TABLE v2_source_experts ALTER COLUMN backfill_cursor SET NOT NULL;
CREATE INDEX v2_source_experts_backfill_due_idx
    ON v2_source_experts(source_id, backfill_cursor, expert_id)
    WHERE enabled AND backfill_completed_at IS NULL;
