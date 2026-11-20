ALTER TABLE v2_telegram_events ADD COLUMN reconcile_request_id UUID;

CREATE INDEX v2_telegram_reconcile_request_idx
    ON v2_telegram_events(source_id,reconcile_owner,reconcile_request_id,reconcile_lease_until,id)
    WHERE is_current AND reconcile_request_id IS NOT NULL AND reconcile_acknowledged_at IS NULL;
