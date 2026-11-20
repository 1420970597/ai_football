-- New winner projections use NULL; zero remains a valid numeric Asian line.
-- No historical evidence, settlement calculation, receipt, or quote identity is rewritten.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM v2_current_quotes WHERE
        market_kind='winner' AND line IS NOT NULL AND line<>0 OR
        market_kind<>'winner' AND (line IS NULL OR NOT(line BETWEEN -1000 AND 1000)))
       OR EXISTS (SELECT 1 FROM v2_expert_signals WHERE
        market_kind='winner' AND line IS NOT NULL AND line<>0 OR
        market_kind<>'winner' AND (line IS NULL OR NOT(line BETWEEN -1000 AND 1000))) THEN
        RAISE EXCEPTION 'explicit_line_preflight_invalid_legacy_projection';
    END IF;
    IF EXISTS (SELECT 1 FROM v2_quote_evidence WHERE
        line IS NOT NULL AND NOT(line BETWEEN -1000 AND 1000) OR
        line IS NULL AND COALESCE(evidence->>'market_kind','')<>'winner') THEN
        RAISE EXCEPTION 'explicit_line_preflight_invalid_legacy_evidence';
    END IF;
    IF EXISTS (SELECT 1 FROM v2_current_quotes GROUP BY fixture_id,market_id,period,COALESCE(line,0),selection HAVING count(*)>1)
       OR EXISTS (SELECT 1 FROM v2_expert_signals GROUP BY message_id,fixture_id,market_id,period,COALESCE(line,0),selection HAVING count(*)>1) THEN
        RAISE EXCEPTION 'explicit_line_preflight_duplicate_semantic_reference';
    END IF;
END $$;
ALTER TABLE v2_current_quotes ALTER COLUMN line DROP NOT NULL;
ALTER TABLE v2_current_quotes ALTER COLUMN line DROP DEFAULT;
ALTER TABLE v2_expert_signals ALTER COLUMN line DROP NOT NULL;
ALTER TABLE v2_expert_signals ALTER COLUMN line DROP DEFAULT;
ALTER TABLE v2_quote_evidence ALTER COLUMN line DROP NOT NULL;
ALTER TABLE v2_quote_evidence ALTER COLUMN line DROP DEFAULT;

-- Legacy winner zero and new winner NULL share one semantic reference.
DROP INDEX v2_quotes_reference_unique;
CREATE UNIQUE INDEX v2_quotes_reference_unique
    ON v2_current_quotes(fixture_id,market_id,period,(COALESCE(line,0)),selection);
DROP INDEX v2_signal_reference_unique;
CREATE UNIQUE INDEX v2_signal_reference_unique
    ON v2_expert_signals(message_id,fixture_id,market_id,period,(COALESCE(line,0)),selection);

-- Preflight blocks incompatible old rows; every existing/new row is then validated.
ALTER TABLE v2_current_quotes ADD CONSTRAINT v2_quotes_explicit_line_check CHECK (
    (market_kind='winner' AND (line IS NULL OR line=0)) OR
    (market_kind<>'winner' AND line IS NOT NULL AND line BETWEEN -1000 AND 1000)
);
ALTER TABLE v2_expert_signals ADD CONSTRAINT v2_signals_explicit_line_check CHECK (
    (market_kind='winner' AND (line IS NULL OR line=0)) OR
    (market_kind<>'winner' AND line IS NOT NULL AND line BETWEEN -1000 AND 1000)
);
ALTER TABLE v2_quote_evidence ADD CONSTRAINT v2_evidence_explicit_line_check CHECK (
    (line IS NOT NULL AND line BETWEEN -1000 AND 1000) OR
    (line IS NULL AND COALESCE(evidence->>'market_kind','')='winner')
);
