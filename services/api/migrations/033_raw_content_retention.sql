-- Raw contents expire independently of durable parsed decisions. Existing
-- immutable decision, quote and result ledgers are deliberately unchanged.
ALTER TABLE v2_expert_messages ADD COLUMN raw_content_expired_at TIMESTAMPTZ;
ALTER TABLE v2_telegram_events ADD COLUMN raw_content_expired_at TIMESTAMPTZ;
ALTER TABLE v2_telegram_events ADD COLUMN message_posted_at TIMESTAMPTZ;
ALTER TABLE v2_fixtures ADD COLUMN decision_context_only_at TIMESTAMPTZ;
CREATE INDEX v2_messages_raw_expiry_idx
    ON v2_expert_messages(posted_at,id)
    WHERE raw_content_expired_at IS NULL;
CREATE INDEX v2_events_raw_expiry_idx ON v2_telegram_events(message_posted_at,id)
    WHERE raw_content_expired_at IS NULL;
CREATE INDEX v2_fixtures_raw_expiry_idx ON v2_fixtures(kickoff_at,id)
    WHERE decision_context_only_at IS NULL;
CREATE INDEX v2_fixtures_archived_gc_idx ON v2_fixtures(kickoff_at,id)
    WHERE decision_context_only_at IS NOT NULL;
-- Expiry deletes check incoming foreign keys for every affected row. The
-- existing compound indexes start with job_id, not the referenced ID.
CREATE INDEX v2_review_market_reads_quote_idx ON v2_review_market_reads(quote_id);
CREATE INDEX v2_review_bindings_fixture_idx ON v2_review_fixture_bindings(fixture_id);
CREATE INDEX v2_match_candidates_fixture_gc_idx ON v2_match_search_candidates(fixture_id);
CREATE INDEX v2_quote_evidence_fixture_idx ON v2_quote_evidence(fixture_id);
CREATE TABLE v2_raw_retention_media (
    object_key TEXT PRIMARY KEY CHECK (length(object_key)<=1024),
    source_id UUID NOT NULL REFERENCES v2_expert_sources(id),
    queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_by UUID,
    claim_until TIMESTAMPTZ
);
CREATE INDEX v2_raw_retention_media_source_idx ON v2_raw_retention_media(source_id,queued_at);
CREATE TABLE v2_raw_retention_status (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    last_started_at TIMESTAMPTZ,
    last_completed_at TIMESTAMPTZ,
    last_error_category TEXT,
    last_counts JSONB NOT NULL DEFAULT '{}'::jsonb
);
INSERT INTO v2_raw_retention_status(singleton) VALUES(TRUE);

CREATE FUNCTION v2_queue_retention_media(source UUID, items JSONB) RETURNS VOID
LANGUAGE plpgsql AS $$
BEGIN
    IF jsonb_typeof(items)='array' THEN
        INSERT INTO v2_raw_retention_media(object_key,source_id)
        SELECT DISTINCT item->>'object_key',source FROM jsonb_array_elements(items) item
        WHERE item->>'object_key' LIKE 'v2/telegram/messages/%'
          AND length(item->>'object_key')<=1024
          AND NOT '..'=ANY(string_to_array(item->>'object_key','/'))
        ON CONFLICT(object_key) DO NOTHING;
    END IF;
END $$;

CREATE FUNCTION v2_expire_raw_message() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.posted_at<clock_timestamp()-interval '72 hours' THEN
        IF TG_OP='UPDATE' THEN PERFORM v2_queue_retention_media(OLD.source_id,OLD.media_objects); END IF;
        PERFORM v2_queue_retention_media(NEW.source_id,NEW.media_objects);
        NEW.raw_text='';
        NEW.reply_context='{}'::jsonb;
        NEW.media_objects='[]'::jsonb;
        NEW.media_state='unsupported';
        NEW.raw_content_expired_at=COALESCE(NEW.raw_content_expired_at,clock_timestamp());
    ELSE
        NEW.raw_content_expired_at=NULL;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER v2_raw_message_ttl BEFORE INSERT OR UPDATE ON v2_expert_messages
    FOR EACH ROW EXECUTE FUNCTION v2_expire_raw_message();

CREATE FUNCTION v2_expire_raw_event() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.message_posted_at=COALESCE((NEW.content->>'posted_at')::timestamptz,NEW.message_posted_at,NEW.source_version_at);
    IF NEW.message_posted_at<clock_timestamp()-interval '72 hours' THEN
        IF TG_OP='UPDATE' THEN PERFORM v2_queue_retention_media(OLD.source_id,OLD.media_objects); END IF;
        PERFORM v2_queue_retention_media(NEW.source_id,NEW.media_objects);
        NEW.content='{}'::jsonb;
        NEW.media_objects='[]'::jsonb;
        NEW.media_state='unsupported';
        NEW.last_error='raw_content_expired';
        NEW.raw_content_expired_at=COALESCE(NEW.raw_content_expired_at,clock_timestamp());
    ELSE
        NEW.raw_content_expired_at=NULL;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER v2_raw_event_ttl BEFORE INSERT OR UPDATE ON v2_telegram_events
    FOR EACH ROW EXECUTE FUNCTION v2_expire_raw_event();

CREATE FUNCTION v2_expire_fixture_projection() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.kickoff_at<clock_timestamp()-interval '72 hours' THEN
        -- Only the small normalized context of referenced parsed decisions may
        -- survive the sweeper. No team aliases, clock or provider team IDs.
        NEW.home_aliases='{}'; NEW.away_aliases='{}';
        NEW.home_team_id=NULL; NEW.away_team_id=NULL;
        NEW.competition_id=NULL; NEW.clock_minute=NULL;
        NEW.decision_context_only_at=COALESCE(NEW.decision_context_only_at,clock_timestamp());
    ELSE
        NEW.decision_context_only_at=NULL;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER v2_fixture_projection_ttl BEFORE INSERT OR UPDATE ON v2_fixtures
    FOR EACH ROW EXECUTE FUNCTION v2_expire_fixture_projection();

CREATE FUNCTION v2_reject_expired_quote() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM v2_fixtures WHERE id=NEW.fixture_id
              AND decision_context_only_at IS NOT NULL) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER v2_quote_projection_ttl BEFORE INSERT OR UPDATE ON v2_current_quotes
    FOR EACH ROW EXECUTE FUNCTION v2_reject_expired_quote();

CREATE FUNCTION v2_reject_expired_review_content() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE expired BOOLEAN;
BEGIN
    IF TG_TABLE_NAME='v2_analysis_jobs' THEN
        SELECT raw_content_expired_at IS NOT NULL INTO expired
          FROM v2_expert_messages WHERE id=NEW.message_id;
        IF expired THEN
            NEW.state='failed'; NEW.lease_until=NULL; NEW.last_error='raw_content_expired';
        END IF;
    ELSE
        SELECT m.raw_content_expired_at IS NOT NULL INTO expired
          FROM v2_analysis_jobs j JOIN v2_expert_messages m ON m.id=j.message_id WHERE j.id=NEW.job_id;
        IF expired THEN
            NEW.payload=jsonb_build_object('retention','raw_content_expired');
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER v2_review_job_raw_ttl BEFORE INSERT OR UPDATE ON v2_analysis_jobs
    FOR EACH ROW EXECUTE FUNCTION v2_reject_expired_review_content();
CREATE TRIGGER v2_review_trace_raw_ttl BEFORE INSERT OR UPDATE ON v2_analysis_traces
    FOR EACH ROW EXECUTE FUNCTION v2_reject_expired_review_content();

COMMENT ON COLUMN v2_fixtures.decision_context_only_at IS
    'Raw projection expired: retain only normalized context while durable decision/evidence foreign keys exist; exclude from current fixture lists.';
