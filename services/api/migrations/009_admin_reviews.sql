-- Preserve every prior review and trace; a manual retry creates a new job.
CREATE TABLE v2_admin_review_retries (
    id UUID PRIMARY KEY,
    actor_id UUID NOT NULL REFERENCES v2_users(id),
    idempotency_key UUID NOT NULL,
    original_job_id UUID NOT NULL REFERENCES v2_analysis_jobs(id),
    retry_job_id UUID NOT NULL UNIQUE REFERENCES v2_analysis_jobs(id),
    reason TEXT NOT NULL CHECK(length(btrim(reason)) BETWEEN 3 AND 500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(actor_id,idempotency_key),
    CHECK(original_job_id<>retry_job_id)
);
CREATE INDEX v2_admin_review_retries_origin_idx ON v2_admin_review_retries(original_job_id,created_at DESC,id DESC);
CREATE UNIQUE INDEX v2_analysis_jobs_one_active_message_idx
    ON v2_analysis_jobs(message_id) WHERE state IN ('queued','running','retry');
CREATE INDEX v2_analysis_jobs_state_created_idx ON v2_analysis_jobs(state,created_at DESC,id DESC);
CREATE INDEX v2_analysis_jobs_message_created_idx ON v2_analysis_jobs(message_id,created_at DESC,id DESC);
