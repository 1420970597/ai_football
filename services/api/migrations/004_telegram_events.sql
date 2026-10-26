CREATE TABLE IF NOT EXISTS v2_telegram_events (
    id UUID PRIMARY KEY,
    source_id UUID NOT NULL REFERENCES v2_expert_sources(id) ON DELETE CASCADE,
    platform_message_id TEXT NOT NULL,
    version_key TEXT NOT NULL,
    source_version_at TIMESTAMPTZ NOT NULL,
    event_kind TEXT NOT NULL CHECK (event_kind IN ('message','edit','delete')),
    content JSONB NOT NULL,
    media_objects JSONB NOT NULL DEFAULT '[]'::jsonb,
    media_state TEXT NOT NULL CHECK (media_state IN ('ready','pending','failed','unsupported')),
    message_id UUID REFERENCES v2_expert_messages(id),
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    last_error TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(source_id,platform_message_id,version_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS v2_telegram_events_current_unique
    ON v2_telegram_events(source_id,platform_message_id) WHERE is_current;
CREATE INDEX IF NOT EXISTS v2_telegram_events_pending_idx
    ON v2_telegram_events(media_state,updated_at) WHERE is_current AND media_state IN ('pending','failed');
CREATE TABLE IF NOT EXISTS v2_telegram_event_members (
    event_id UUID NOT NULL REFERENCES v2_telegram_events(id) ON DELETE CASCADE,
    platform_message_id BIGINT NOT NULL,
    PRIMARY KEY(event_id,platform_message_id)
);
ALTER TABLE v2_source_checkpoints ADD COLUMN IF NOT EXISTS collector_owner UUID;
ALTER TABLE v2_source_checkpoints ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
ALTER TABLE v2_expert_signals ADD COLUMN IF NOT EXISTS withdrawn_at TIMESTAMPTZ;
ALTER TABLE v2_expert_signals ADD COLUMN IF NOT EXISTS withdrawal_reason TEXT;
CREATE TABLE IF NOT EXISTS v2_telegram_tombstones (
    source_id UUID NOT NULL REFERENCES v2_expert_sources(id) ON DELETE CASCADE,
    platform_message_id BIGINT NOT NULL,
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(source_id,platform_message_id)
);
ALTER TABLE v2_expert_messages ADD COLUMN IF NOT EXISTS reply_context JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE v2_telegram_events ADD COLUMN IF NOT EXISTS next_check_at TIMESTAMPTZ NOT NULL DEFAULT now()+interval '5 minutes';
CREATE INDEX IF NOT EXISTS v2_telegram_events_reconcile_idx ON v2_telegram_events(source_id,next_check_at,id) WHERE is_current;
