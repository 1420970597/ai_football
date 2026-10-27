ALTER TABLE v2_current_quotes ADD COLUMN score_basis TEXT NOT NULL DEFAULT 'unknown'
    CHECK (score_basis IN ('unknown','full_period','remaining_period'));
ALTER TABLE v2_expert_signals ADD COLUMN score_basis TEXT NOT NULL DEFAULT 'unknown'
    CHECK (score_basis IN ('unknown','full_period','remaining_period'));
ALTER TABLE v2_expert_signals ADD COLUMN effective_stake NUMERIC(12,4);
ALTER TABLE v2_expert_signals ADD COLUMN settlement_reason TEXT NOT NULL DEFAULT 'result_not_confirmed';
ALTER TABLE v2_expert_signals ADD COLUMN result_version INTEGER;
ALTER TABLE v2_review_market_reads ADD COLUMN quote_context JSONB;
ALTER TABLE v2_current_quotes ALTER COLUMN price TYPE NUMERIC(12,5);
ALTER TABLE v2_quote_evidence ALTER COLUMN price TYPE NUMERIC(12,5);
ALTER TABLE v2_review_market_reads ALTER COLUMN price TYPE NUMERIC(12,5);
ALTER TABLE v2_expert_signals ALTER COLUMN recommendation_price TYPE NUMERIC(12,5);
ALTER TABLE v2_expert_signals ALTER COLUMN unit_net TYPE NUMERIC(16,6);
ALTER TABLE v2_bet_orders ALTER COLUMN price TYPE NUMERIC(12,5);
CREATE TABLE v2_fixture_results (
    fixture_id TEXT NOT NULL REFERENCES v2_fixtures(id),
    version INTEGER NOT NULL CHECK (version > 0),
    provider_reference TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('provisional','confirmed','void')),
    home_score INTEGER CHECK (home_score >= 0),
    away_score INTEGER CHECK (away_score >= 0),
    half_home_score INTEGER CHECK (half_home_score >= 0),
    half_away_score INTEGER CHECK (half_away_score >= 0),
    source_updated_at TIMESTAMPTZ NOT NULL,
    evidence JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(fixture_id,version),
    UNIQUE(fixture_id,provider_reference,content_hash)
);
CREATE TABLE v2_signal_settlements (
    signal_id UUID NOT NULL REFERENCES v2_expert_signals(id),
    fixture_id TEXT NOT NULL,
    result_version INTEGER NOT NULL,
    algorithm_version TEXT NOT NULL,
    outcome TEXT NOT NULL,
    unit_net NUMERIC(16,6),
    effective_stake NUMERIC(12,4) NOT NULL CHECK (effective_stake BETWEEN 0 AND 1),
    reason TEXT NOT NULL,
    calculation JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(signal_id,result_version,algorithm_version),
    FOREIGN KEY(fixture_id,result_version) REFERENCES v2_fixture_results(fixture_id,version)
);
CREATE INDEX v2_results_latest_idx ON v2_fixture_results(fixture_id,version DESC);
CREATE INDEX v2_signals_expert_settled_idx ON v2_expert_signals(expert_id,created_at,id)
    INCLUDE (outcome,unit_net,effective_stake);
