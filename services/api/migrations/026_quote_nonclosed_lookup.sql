-- Closing complete/terminal snapshots must not scan historical closed quotes.
-- Include suspended options: an active-only index cannot serve this predicate.
CREATE INDEX IF NOT EXISTS v2_quotes_nonclosed_fixture_idx
    ON v2_current_quotes(fixture_id) WHERE status <> 'closed';

-- Production may prepare this exact index concurrently before the locked,
-- transactional migration. Never silently accept a wrong or invalid index.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_index
        WHERE indexrelid = 'public.v2_quotes_nonclosed_fixture_idx'::regclass
          AND indisvalid AND indisready
          AND pg_get_indexdef(indexrelid) =
            'CREATE INDEX v2_quotes_nonclosed_fixture_idx ON public.v2_current_quotes USING btree (fixture_id) WHERE (status <> ''closed''::text)'
    ) THEN
        RAISE EXCEPTION 'nonclosed quote index definition or validity mismatch';
    END IF;
END $$;
