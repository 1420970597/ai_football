-- 原生详情没有源修订时钟；观测时钟必须独立保存，不伪造source_updated_at。
ALTER TABLE v2_fixture_results ALTER COLUMN source_updated_at DROP NOT NULL;
ALTER TABLE v2_fixture_results ADD COLUMN observed_at TIMESTAMPTZ;
ALTER TABLE v2_fixture_results ADD CONSTRAINT v2_result_clock_required
    CHECK (source_updated_at IS NOT NULL OR observed_at IS NOT NULL);
CREATE TABLE v2_result_checks (
    fixture_id TEXT PRIMARY KEY REFERENCES v2_fixtures(id),
    lease_id UUID,
    lease_until TIMESTAMPTZ,
    next_check_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    failures INTEGER NOT NULL DEFAULT 0,
    last_reason TEXT NOT NULL DEFAULT '',
    last_checked_at TIMESTAMPTZ
);
CREATE INDEX v2_result_checks_due_idx ON v2_result_checks(next_check_at,fixture_id);
CREATE INDEX v2_signals_result_discovery_idx ON v2_expert_signals(created_at DESC,id) INCLUDE(fixture_id);
