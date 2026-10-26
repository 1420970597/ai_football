CREATE TABLE IF NOT EXISTS v2_configuration_revisions (
    namespace TEXT PRIMARY KEY,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO v2_configuration_revisions(namespace) VALUES ('leyu') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS v2_renewal_accounts (
    account_hash TEXT PRIMARY KEY CHECK (account_hash ~ '^[a-f0-9]{64}$'),
    state TEXT NOT NULL DEFAULT 'ready' CHECK (state IN ('ready','running','unknown','rejected')),
    active_attempt UUID,
    lease_until TIMESTAMPTZ,
    retry_after TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS v2_renewal_attempts (
    id UUID PRIMARY KEY,
    account_hash TEXT NOT NULL REFERENCES v2_renewal_accounts(account_hash),
    config_revision BIGINT NOT NULL,
    state TEXT NOT NULL DEFAULT 'running' CHECK (state IN ('running','success','failed','unknown','rejected')),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS v2_renewal_attempts_account_time_idx ON v2_renewal_attempts(account_hash,started_at DESC);
