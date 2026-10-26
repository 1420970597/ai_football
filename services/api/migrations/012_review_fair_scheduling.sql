-- Scheduling metadata supplements the original queue; it is not another queue.
ALTER TABLE v2_analysis_jobs ADD COLUMN source_id UUID REFERENCES v2_expert_sources(id);
ALTER TABLE v2_analysis_jobs ADD COLUMN expert_id UUID REFERENCES v2_experts(id);
UPDATE v2_analysis_jobs j SET source_id=m.source_id,expert_id=m.expert_id FROM v2_expert_messages m WHERE m.id=j.message_id;
ALTER TABLE v2_analysis_jobs ALTER COLUMN source_id SET NOT NULL;
ALTER TABLE v2_analysis_jobs ALTER COLUMN expert_id SET NOT NULL;
ALTER TABLE v2_expert_messages ADD CONSTRAINT v2_review_message_schedule_key UNIQUE(id,source_id,expert_id);
ALTER TABLE v2_analysis_jobs ADD CONSTRAINT v2_review_job_message_schedule_fk
    FOREIGN KEY(message_id,source_id,expert_id) REFERENCES v2_expert_messages(id,source_id,expert_id);

CREATE FUNCTION v2_review_schedule_keys() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    SELECT source_id,expert_id INTO NEW.source_id,NEW.expert_id FROM v2_expert_messages WHERE id=NEW.message_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'review message reference missing' USING ERRCODE='23503';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER v2_review_schedule_keys_trigger
    BEFORE INSERT OR UPDATE OF message_id,source_id,expert_id ON v2_analysis_jobs
    FOR EACH ROW EXECUTE FUNCTION v2_review_schedule_keys();

CREATE TABLE v2_review_source_schedule (
    source_id UUID PRIMARY KEY REFERENCES v2_expert_sources(id),
    last_turn BIGINT NOT NULL DEFAULT 0 CHECK(last_turn>=0)
);
CREATE TABLE v2_review_expert_schedule (
    expert_id UUID PRIMARY KEY REFERENCES v2_experts(id),
    concurrency_limit INTEGER NOT NULL DEFAULT 1 CHECK(concurrency_limit BETWEEN 1 AND 8),
    last_turn BIGINT NOT NULL DEFAULT 0 CHECK(last_turn>=0)
);
INSERT INTO v2_review_source_schedule(source_id) SELECT id FROM v2_expert_sources;
INSERT INTO v2_review_expert_schedule(expert_id) SELECT id FROM v2_experts;
CREATE INDEX v2_review_source_turn_idx ON v2_review_source_schedule(last_turn,source_id);
CREATE INDEX v2_review_expert_turn_idx ON v2_review_expert_schedule(last_turn,expert_id);
CREATE INDEX v2_review_queued_pair_idx ON v2_analysis_jobs(source_id,expert_id,available_at,created_at,id)
    WHERE state IN ('queued','retry');
CREATE INDEX v2_review_expired_pair_idx ON v2_analysis_jobs(source_id,expert_id,lease_until,created_at,id)
    WHERE state='running';
CREATE INDEX v2_review_expert_leases_idx ON v2_analysis_jobs(expert_id,lease_until) WHERE state='running';
