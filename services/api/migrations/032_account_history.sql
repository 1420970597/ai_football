-- Read-only personal account snapshots; existing job leases and binding locks apply.
ALTER TABLE v2_account_jobs DROP CONSTRAINT v2_account_jobs_kind_check;
ALTER TABLE v2_account_jobs ADD CONSTRAINT v2_account_jobs_kind_check
 CHECK(kind IN('verify','refresh','reconcile','history'));

CREATE TABLE v2_account_history (
 account_id UUID PRIMARY KEY REFERENCES v2_leyu_accounts(id),
 user_id UUID NOT NULL REFERENCES v2_users(id),
 binding_revision BIGINT NOT NULL,
 job_id UUID NOT NULL REFERENCES v2_account_jobs(id),
 window_start TIMESTAMPTZ NOT NULL,
 window_end TIMESTAMPTZ NOT NULL,
 requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 observed_at TIMESTAMPTZ,
 complete BOOLEAN NOT NULL DEFAULT false,
 items JSONB NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(items)='array'),
 error_category TEXT NOT NULL DEFAULT '',
 CHECK(window_end > window_start AND window_end-window_start=interval '7 days')
);
