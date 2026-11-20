-- Existing user permissions remain manual until the account owner saves a mode.
ALTER TABLE v2_user_bet_settings ADD COLUMN expert_selection_mode TEXT NOT NULL DEFAULT 'manual'
    CHECK (expert_selection_mode IN ('manual','profitable'));

-- Original signals, settlement ledger and real orders are never removed.
CREATE TABLE v2_expert_statistics_resets (
    id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 120),
    created_at TIMESTAMPTZ NOT NULL,
    actor_id UUID REFERENCES v2_users(id),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    before_summary JSONB NOT NULL,
    revoked_at TIMESTAMPTZ,
    revocation_reason TEXT,
    CHECK ((revoked_at IS NULL) = (revocation_reason IS NULL))
);
CREATE TABLE v2_expert_statistics_reset_experts (
    reset_id UUID NOT NULL REFERENCES v2_expert_statistics_resets(id),
    expert_id UUID NOT NULL REFERENCES v2_experts(id),
    PRIMARY KEY(reset_id,expert_id)
);
CREATE TABLE v2_expert_statistics_reset_signals (
    reset_id UUID NOT NULL REFERENCES v2_expert_statistics_resets(id),
    signal_id UUID NOT NULL REFERENCES v2_expert_signals(id),
    PRIMARY KEY(reset_id,signal_id)
);
CREATE INDEX v2_expert_statistics_reset_signal_lookup
    ON v2_expert_statistics_reset_signals(signal_id,reset_id);
CREATE INDEX v2_expert_statistics_reset_expert_lookup
    ON v2_expert_statistics_reset_experts(expert_id,reset_id);
