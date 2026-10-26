ALTER TABLE v2_users ADD COLUMN IF NOT EXISTS totp_last_step BIGINT;
CREATE TABLE IF NOT EXISTS v2_auth_rate_limits (
    bucket_key TEXT PRIMARY KEY,
    window_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    blocked_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE v2_memberships ADD COLUMN IF NOT EXISTS grant_reason TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS v2_memberships_grant_reference_unique
    ON v2_memberships(grant_source, external_reference) WHERE external_reference IS NOT NULL;

ALTER TABLE v2_fixtures ALTER COLUMN source_updated_at DROP NOT NULL;
ALTER TABLE v2_current_quotes ALTER COLUMN source_updated_at DROP NOT NULL;
ALTER TABLE v2_current_quotes ALTER COLUMN price DROP NOT NULL;
ALTER TABLE v2_current_quotes ADD COLUMN IF NOT EXISTS period TEXT NOT NULL DEFAULT 'full_time';
ALTER TABLE v2_current_quotes ADD COLUMN IF NOT EXISTS play_id TEXT;
ALTER TABLE v2_current_quotes ADD COLUMN IF NOT EXISTS market_instance_id TEXT;
ALTER TABLE v2_current_quotes ADD COLUMN IF NOT EXISTS selection_id TEXT;
ALTER TABLE v2_current_quotes ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'suspended', 'closed'));
ALTER TABLE v2_quote_evidence ADD COLUMN IF NOT EXISTS period TEXT NOT NULL DEFAULT 'full_time';
ALTER TABLE v2_quote_evidence ADD COLUMN IF NOT EXISTS score_baseline JSONB;
ALTER TABLE v2_expert_signals ADD COLUMN IF NOT EXISTS period TEXT NOT NULL DEFAULT 'full_time';
ALTER TABLE v2_expert_signals ADD COLUMN IF NOT EXISTS score_baseline JSONB;
CREATE INDEX IF NOT EXISTS v2_current_quotes_active_idx
    ON v2_current_quotes(fixture_id, source_updated_at DESC) WHERE status='active';
