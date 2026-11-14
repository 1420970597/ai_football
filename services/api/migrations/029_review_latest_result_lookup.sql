-- Diagnostics reads only the latest persisted framework result. Keep bulky
-- payloads out of this partial index and permit an exact concurrent prebuild.
CREATE INDEX IF NOT EXISTS v2_review_latest_result_idx
    ON v2_analysis_traces(created_at DESC) WHERE trace_kind='hermes_result';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_index
        WHERE indexrelid = 'public.v2_review_latest_result_idx'::regclass
          AND indisvalid AND indisready
          AND pg_get_indexdef(indexrelid) =
            'CREATE INDEX v2_review_latest_result_idx ON public.v2_analysis_traces USING btree (created_at DESC) WHERE (trace_kind = ''hermes_result''::text)'
    ) THEN
        RAISE EXCEPTION 'latest review result index definition or validity mismatch';
    END IF;
END $$;
