-- Original Telegram posting time is an indexed projection, never receipt time.
ALTER TABLE v2_analysis_jobs ADD COLUMN message_posted_at TIMESTAMPTZ;
UPDATE v2_analysis_jobs j SET message_posted_at=m.posted_at
    FROM v2_expert_messages m WHERE m.id=j.message_id;
ALTER TABLE v2_analysis_jobs ALTER COLUMN message_posted_at SET NOT NULL;
ALTER TABLE v2_expert_messages ADD CONSTRAINT v2_review_message_posted_key UNIQUE(id,posted_at);
ALTER TABLE v2_analysis_jobs ADD CONSTRAINT v2_review_job_posted_fk
    FOREIGN KEY(message_id,message_posted_at) REFERENCES v2_expert_messages(id,posted_at);
ALTER TABLE v2_analysis_jobs ADD COLUMN review_mode TEXT NOT NULL DEFAULT 'real_time'
    CHECK(review_mode IN ('real_time','historical_replay'));
-- Existing attempts retain their ownership/state; old posting dates are not
-- silently reinterpreted as new recommendations during the deployment window.
UPDATE v2_analysis_jobs SET review_mode='historical_replay'
    WHERE message_posted_at<statement_timestamp()-interval '24 hours';

CREATE OR REPLACE FUNCTION v2_review_schedule_keys() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    SELECT source_id,expert_id,posted_at INTO NEW.source_id,NEW.expert_id,NEW.message_posted_at
        FROM v2_expert_messages WHERE id=NEW.message_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'review message reference missing' USING ERRCODE='23503';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER v2_review_schedule_keys_trigger ON v2_analysis_jobs;
CREATE TRIGGER v2_review_schedule_keys_trigger
    BEFORE INSERT OR UPDATE OF message_id,source_id,expert_id,message_posted_at ON v2_analysis_jobs
    FOR EACH ROW EXECUTE FUNCTION v2_review_schedule_keys();

-- Scheduling counters survive worker restarts and are held under the existing
-- transaction-level scheduler advisory lock, shared by every worker process.
CREATE TABLE v2_review_lane_schedule (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    realtime_claims_since_replay INTEGER NOT NULL DEFAULT 0 CHECK(realtime_claims_since_replay>=0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO v2_review_lane_schedule(singleton) VALUES(TRUE);

CREATE INDEX v2_review_ready_fresh_pair_idx
    ON v2_analysis_jobs(source_id,expert_id,message_posted_at DESC,available_at,created_at,id)
    WHERE state IN ('queued','retry');
CREATE INDEX v2_review_expired_fresh_pair_idx
    ON v2_analysis_jobs(source_id,expert_id,message_posted_at DESC,lease_until,created_at,id)
    WHERE state='running';
CREATE INDEX v2_review_running_mode_idx ON v2_analysis_jobs(review_mode,lease_until)
    WHERE state='running';
