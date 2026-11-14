CREATE TABLE v2_telegram_directories (
    source_id UUID PRIMARY KEY REFERENCES v2_expert_sources(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('ready','unavailable','flood_wait')),
    reason TEXT,
    retry_after INTEGER,
    admins JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_attempt_at TIMESTAMPTZ NOT NULL,
    last_success_at TIMESTAMPTZ
);
CREATE TABLE v2_expert_nickname_history (
    expert_id UUID NOT NULL REFERENCES v2_experts(id) ON DELETE CASCADE,
    nickname TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(expert_id,nickname)
);
