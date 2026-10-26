CREATE TABLE v2_admin_mfa_enrollments (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES v2_users(id),
    session_hash TEXT NOT NULL REFERENCES v2_sessions(token_hash) ON DELETE CASCADE,
    encrypted_secret TEXT NOT NULL,
    previous_secret_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX v2_admin_mfa_enrollments_active_idx ON v2_admin_mfa_enrollments(user_id)
    WHERE consumed_at IS NULL AND revoked_at IS NULL;
CREATE INDEX v2_admin_mfa_enrollments_session_idx ON v2_admin_mfa_enrollments(session_hash);
