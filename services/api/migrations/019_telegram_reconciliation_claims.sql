-- A read is not an acknowledgment: keep due time until the checked event is committed.
ALTER TABLE v2_telegram_events ADD COLUMN reconcile_claim_id UUID;
ALTER TABLE v2_telegram_events ADD COLUMN reconcile_owner UUID;
ALTER TABLE v2_telegram_events ADD COLUMN reconcile_lease_until TIMESTAMPTZ;
ALTER TABLE v2_telegram_events ADD COLUMN reconcile_applied_at TIMESTAMPTZ;
ALTER TABLE v2_telegram_events ADD COLUMN reconcile_applied_sha256 TEXT CHECK (reconcile_applied_sha256 ~ '^[a-f0-9]{64}$');
ALTER TABLE v2_telegram_events ADD COLUMN reconcile_acknowledged_at TIMESTAMPTZ;
ALTER TABLE v2_telegram_events ADD CONSTRAINT v2_telegram_reconcile_claim_complete CHECK (
    (reconcile_claim_id IS NULL AND reconcile_owner IS NULL AND reconcile_lease_until IS NULL
     AND reconcile_applied_at IS NULL AND reconcile_applied_sha256 IS NULL AND reconcile_acknowledged_at IS NULL)
    OR (reconcile_claim_id IS NOT NULL AND reconcile_owner IS NOT NULL AND reconcile_lease_until IS NOT NULL
        AND (reconcile_applied_at IS NULL) = (reconcile_applied_sha256 IS NULL)
        AND (reconcile_acknowledged_at IS NULL OR reconcile_applied_at IS NOT NULL))
);
CREATE INDEX v2_telegram_reconcile_claim_due_idx
    ON v2_telegram_events(source_id,next_check_at,reconcile_lease_until,id) WHERE is_current;
