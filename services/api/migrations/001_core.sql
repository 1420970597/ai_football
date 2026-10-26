CREATE TABLE IF NOT EXISTS v2_users (
    id UUID PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'admin')),
    totp_secret_ciphertext TEXT,
    disabled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS v2_sessions (
    token_hash TEXT PRIMARY KEY,
    csrf_hash TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES v2_users(id) ON DELETE CASCADE,
    session_kind TEXT NOT NULL CHECK (session_kind IN ('user', 'admin')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS v2_sessions_user_expiry_idx ON v2_sessions(user_id, expires_at);

CREATE TABLE IF NOT EXISTS v2_membership_plans (
    id UUID PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    price NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (price >= 0),
    currency CHAR(3) NOT NULL DEFAULT 'CNY',
    duration_days INTEGER NOT NULL CHECK (duration_days > 0),
    entitlements JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS v2_memberships (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES v2_users(id),
    plan_id UUID NOT NULL REFERENCES v2_membership_plans(id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'expired', 'cancelled')),
    starts_at TIMESTAMPTZ,
    ends_at TIMESTAMPTZ,
    grant_source TEXT NOT NULL,
    external_reference TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS v2_memberships_user_status_idx ON v2_memberships(user_id, status, ends_at DESC);

CREATE TABLE IF NOT EXISTS v2_expert_sources (
    id UUID PRIMARY KEY,
    platform_chat_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    allow_media BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS v2_experts (
    id UUID PRIMARY KEY,
    platform_sender_id TEXT NOT NULL,
    display_nickname TEXT NOT NULL,
    avatar_object_key TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(platform_sender_id)
);

CREATE TABLE IF NOT EXISTS v2_source_experts (
    source_id UUID NOT NULL REFERENCES v2_expert_sources(id) ON DELETE CASCADE,
    expert_id UUID NOT NULL REFERENCES v2_experts(id) ON DELETE CASCADE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(source_id, expert_id)
);

CREATE TABLE IF NOT EXISTS v2_fixtures (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE,
    sport TEXT NOT NULL,
    competition_id TEXT,
    competition_name TEXT NOT NULL,
    home_team_id TEXT,
    home_team TEXT NOT NULL,
    home_aliases TEXT[] NOT NULL DEFAULT '{}',
    away_team_id TEXT,
    away_team TEXT NOT NULL,
    away_aliases TEXT[] NOT NULL DEFAULT '{}',
    kickoff_at TIMESTAMPTZ NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('scheduled', 'live', 'finished', 'cancelled')),
    match_state TEXT NOT NULL DEFAULT 'scheduled',
    home_score INTEGER,
    away_score INTEGER,
    clock_minute INTEGER,
    source_updated_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS v2_fixtures_phase_kickoff_idx ON v2_fixtures(phase, kickoff_at, id);
CREATE INDEX IF NOT EXISTS v2_fixtures_home_lookup_idx ON v2_fixtures(lower(home_team), kickoff_at);
CREATE INDEX IF NOT EXISTS v2_fixtures_away_lookup_idx ON v2_fixtures(lower(away_team), kickoff_at);
CREATE INDEX IF NOT EXISTS v2_fixtures_home_aliases_idx ON v2_fixtures USING GIN(home_aliases);
CREATE INDEX IF NOT EXISTS v2_fixtures_away_aliases_idx ON v2_fixtures USING GIN(away_aliases);

CREATE TABLE IF NOT EXISTS v2_current_quotes (
    id UUID PRIMARY KEY,
    fixture_id TEXT NOT NULL REFERENCES v2_fixtures(id) ON DELETE CASCADE,
    market_id TEXT NOT NULL,
    market_name TEXT NOT NULL,
    line NUMERIC(8,3) NOT NULL DEFAULT 0,
    selection TEXT NOT NULL,
    price NUMERIC(10,4) NOT NULL CHECK (price > 1),
    source_updated_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(fixture_id, market_id, line, selection)
);
CREATE INDEX IF NOT EXISTS v2_current_quotes_fixture_fresh_idx ON v2_current_quotes(fixture_id, source_updated_at DESC);

CREATE TABLE IF NOT EXISTS v2_quote_evidence (
    id UUID PRIMARY KEY,
    fixture_id TEXT NOT NULL REFERENCES v2_fixtures(id),
    market_id TEXT NOT NULL,
    market_name TEXT NOT NULL,
    line NUMERIC(8,3) NOT NULL DEFAULT 0,
    selection TEXT NOT NULL,
    price NUMERIC(10,4) NOT NULL CHECK (price > 1),
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('expert_signal', 'order_attempt', 'order_receipt', 'settlement')),
    reference_id UUID NOT NULL,
    source_updated_at TIMESTAMPTZ NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS v2_quote_evidence_reference_idx ON v2_quote_evidence(evidence_kind, reference_id);

CREATE TABLE IF NOT EXISTS v2_expert_messages (
    id UUID PRIMARY KEY,
    source_id UUID NOT NULL REFERENCES v2_expert_sources(id),
    expert_id UUID NOT NULL REFERENCES v2_experts(id),
    platform_message_id TEXT NOT NULL,
    reply_to_platform_message_id TEXT,
    raw_text TEXT NOT NULL DEFAULT '',
    media_objects JSONB NOT NULL DEFAULT '[]'::jsonb,
    posted_at TIMESTAMPTZ NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    edited_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(source_id, platform_message_id, revision)
);
CREATE INDEX IF NOT EXISTS v2_expert_messages_author_time_idx ON v2_expert_messages(expert_id, posted_at DESC);

CREATE TABLE IF NOT EXISTS v2_analysis_jobs (
    id UUID PRIMARY KEY,
    message_id UUID NOT NULL REFERENCES v2_expert_messages(id),
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'succeeded', 'unresolved', 'retry', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_until TIMESTAMPTZ,
    last_error TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS v2_analysis_jobs_claim_idx ON v2_analysis_jobs(state, available_at, created_at);

CREATE TABLE IF NOT EXISTS v2_analysis_traces (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES v2_analysis_jobs(id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL,
    trace_kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(job_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS v2_match_searches (
    job_id UUID NOT NULL REFERENCES v2_analysis_jobs(id) ON DELETE CASCADE,
    search_key TEXT NOT NULL,
    team_query TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('all', 'live', 'scheduled')),
    competition_query TEXT,
    from_utc TIMESTAMPTZ,
    to_utc TIMESTAMPTZ,
    expected_cursor TEXT,
    complete BOOLEAN NOT NULL DEFAULT FALSE,
    page_count INTEGER NOT NULL DEFAULT 0,
    total_candidates INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, search_key)
);

CREATE TABLE IF NOT EXISTS v2_match_search_candidates (
    job_id UUID NOT NULL,
    search_key TEXT NOT NULL,
    fixture_id TEXT NOT NULL REFERENCES v2_fixtures(id) ON DELETE CASCADE,
    PRIMARY KEY (job_id, search_key, fixture_id),
    FOREIGN KEY (job_id, search_key) REFERENCES v2_match_searches(job_id, search_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS v2_match_search_candidates_fixture_idx
    ON v2_match_search_candidates(job_id, fixture_id);

CREATE TABLE IF NOT EXISTS v2_expert_signals (
    id UUID PRIMARY KEY,
    message_id UUID NOT NULL REFERENCES v2_expert_messages(id),
    expert_id UUID NOT NULL REFERENCES v2_experts(id),
    fixture_id TEXT NOT NULL REFERENCES v2_fixtures(id),
    phase TEXT NOT NULL CHECK (phase IN ('scheduled', 'live')),
    market_id TEXT NOT NULL,
    market_name TEXT NOT NULL,
    line NUMERIC(8,3) NOT NULL DEFAULT 0,
    selection TEXT NOT NULL,
    recommendation_price NUMERIC(10,4) NOT NULL,
    quote_time TIMESTAMPTZ NOT NULL,
    confidence NUMERIC(5,4) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    evidence_text TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'pending' CHECK (outcome IN ('pending', 'win', 'loss', 'half_win', 'half_loss', 'push', 'void')),
    unit_net NUMERIC(12,4),
    settled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(message_id, fixture_id, market_id, line, selection)
);
CREATE INDEX IF NOT EXISTS v2_expert_signals_expert_time_idx ON v2_expert_signals(expert_id, created_at DESC);
CREATE INDEX IF NOT EXISTS v2_expert_signals_fixture_idx ON v2_expert_signals(fixture_id, created_at DESC);

CREATE TABLE IF NOT EXISTS v2_leyu_accounts (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL UNIQUE REFERENCES v2_users(id) ON DELETE CASCADE,
    encrypted_credentials TEXT NOT NULL,
    account_label TEXT NOT NULL,
    health_state TEXT NOT NULL DEFAULT 'unverified' CHECK (health_state IN ('unverified', 'healthy', 'renewal_required', 'rejected', 'disabled')),
    last_verified_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS v2_user_bet_settings (
    user_id UUID PRIMARY KEY REFERENCES v2_users(id) ON DELETE CASCADE,
    auto_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    allowed_experts UUID[] NOT NULL DEFAULT '{}',
    stake_per_order NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (stake_per_order >= 0),
    max_per_order NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (max_per_order >= 0),
    daily_limit NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (daily_limit >= 0),
    open_exposure_limit NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (open_exposure_limit >= 0),
    emergency_stopped BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS v2_bet_orders (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES v2_users(id),
    signal_id UUID NOT NULL REFERENCES v2_expert_signals(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('pending', 'submitting', 'accepted', 'rejected', 'unknown', 'settled', 'cancelled')),
    stake NUMERIC(12,2) NOT NULL CHECK (stake > 0),
    price NUMERIC(10,4) NOT NULL CHECK (price > 1),
    leyu_receipt TEXT,
    result_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS v2_bet_orders_user_time_idx ON v2_bet_orders(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS v2_admin_settings (
    setting_key TEXT PRIMARY KEY,
    encrypted_value TEXT NOT NULL,
    updated_by UUID REFERENCES v2_users(id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS v2_audit_log (
    id UUID PRIMARY KEY,
    actor_id UUID REFERENCES v2_users(id),
    actor_role TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS v2_audit_log_time_idx ON v2_audit_log(created_at DESC);

INSERT INTO v2_membership_plans(id, code, name, duration_days, entitlements, enabled)
VALUES ('5dcb2545-dcce-4d22-b226-7ccf16157c01', 'free', '游客与免费用户', 36500,
        '{"public_fixtures": true, "expert_summary": true}'::jsonb, TRUE)
ON CONFLICT (code) DO NOTHING;
