ALTER TABLE v2_sessions ADD COLUMN reauthenticated_at TIMESTAMPTZ;
ALTER TABLE v2_users ADD COLUMN closure_requested_at TIMESTAMPTZ;
ALTER TABLE v2_users ADD COLUMN anonymized_at TIMESTAMPTZ;

CREATE TABLE v2_password_recoveries (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES v2_users(id),
    code_hash TEXT NOT NULL UNIQUE,
    issued_by UUID NOT NULL REFERENCES v2_users(id),
    idempotency_key UUID NOT NULL UNIQUE,
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 3 AND 500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);
CREATE INDEX v2_password_recoveries_user_idx ON v2_password_recoveries(user_id,created_at DESC);

CREATE TABLE v2_admin_recovery_codes (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES v2_users(id),
    code_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);
CREATE INDEX v2_admin_recovery_codes_active_idx ON v2_admin_recovery_codes(user_id) WHERE consumed_at IS NULL AND revoked_at IS NULL;
