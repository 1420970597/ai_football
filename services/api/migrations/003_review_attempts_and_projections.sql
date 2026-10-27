ALTER TABLE v2_fixtures ADD COLUMN IF NOT EXISTS half_home_score INTEGER;
ALTER TABLE v2_fixtures ADD COLUMN IF NOT EXISTS half_away_score INTEGER;
ALTER TABLE v2_fixtures ADD COLUMN IF NOT EXISTS source_time_quality TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE v2_current_quotes ADD COLUMN IF NOT EXISTS market_kind TEXT NOT NULL DEFAULT 'unsupported';
ALTER TABLE v2_current_quotes ADD COLUMN IF NOT EXISTS is_main BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE v2_current_quotes ADD COLUMN IF NOT EXISTS source_time_quality TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE v2_current_quotes DROP CONSTRAINT IF EXISTS v2_current_quotes_fixture_id_market_id_line_selection_key;
CREATE UNIQUE INDEX IF NOT EXISTS v2_quotes_reference_unique
    ON v2_current_quotes(fixture_id, market_id, period, line, selection);

ALTER TABLE v2_analysis_jobs ADD COLUMN IF NOT EXISTS attempt_id UUID;
ALTER TABLE v2_analysis_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
ALTER TABLE v2_match_searches ADD COLUMN IF NOT EXISTS attempt_id UUID;
ALTER TABLE v2_match_search_candidates ADD COLUMN IF NOT EXISTS position BIGINT;
ALTER TABLE v2_match_search_candidates ADD COLUMN IF NOT EXISTS candidate_data JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE INDEX IF NOT EXISTS v2_match_candidates_position_idx
    ON v2_match_search_candidates(job_id, search_key, position);

CREATE TABLE IF NOT EXISTS v2_review_fixture_bindings (
    job_id UUID NOT NULL REFERENCES v2_analysis_jobs(id) ON DELETE CASCADE,
    attempt_id UUID NOT NULL,
    fixture_id TEXT NOT NULL REFERENCES v2_fixtures(id),
    search_key TEXT NOT NULL,
    evidence JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(job_id, attempt_id, fixture_id)
);
CREATE TABLE IF NOT EXISTS v2_review_market_reads (
    job_id UUID NOT NULL REFERENCES v2_analysis_jobs(id) ON DELETE CASCADE,
    attempt_id UUID NOT NULL,
    quote_id UUID NOT NULL REFERENCES v2_current_quotes(id),
    source_updated_at TIMESTAMPTZ,
    price NUMERIC(10,4),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(job_id, attempt_id, quote_id)
);
ALTER TABLE v2_expert_signals ADD COLUMN IF NOT EXISTS market_kind TEXT NOT NULL DEFAULT 'unsupported';
DO $$ DECLARE old_constraint RECORD; BEGIN
    FOR old_constraint IN SELECT conname FROM pg_constraint
        WHERE conrelid='v2_expert_signals'::regclass AND contype='u'
        AND pg_get_constraintdef(oid)='UNIQUE (message_id, fixture_id, market_id, line, selection)'
    LOOP
        EXECUTE format('ALTER TABLE v2_expert_signals DROP CONSTRAINT %I', old_constraint.conname);
    END LOOP;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS v2_signal_reference_unique
    ON v2_expert_signals(message_id, fixture_id, market_id, period, line, selection);
ALTER TABLE v2_expert_messages ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE v2_expert_messages ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE v2_expert_messages ADD COLUMN IF NOT EXISTS media_state TEXT NOT NULL DEFAULT 'ready'
    CHECK (media_state IN ('ready', 'pending', 'failed', 'unsupported'));
CREATE TABLE IF NOT EXISTS v2_source_checkpoints (
    source_id UUID PRIMARY KEY REFERENCES v2_expert_sources(id) ON DELETE CASCADE,
    last_message_id BIGINT NOT NULL DEFAULT 0,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error TEXT
);
