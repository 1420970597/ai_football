-- Read at most the selected summary options for each fixture without sorting
-- all its quotes. Production may prepare this index concurrently beforehand.
CREATE INDEX IF NOT EXISTS v2_quotes_fixture_summary_idx
    ON v2_current_quotes(fixture_id,is_main DESC,source_updated_at DESC NULLS LAST,market_id,line,selection);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_index
        WHERE indexrelid = 'public.v2_quotes_fixture_summary_idx'::regclass
          AND indisvalid AND indisready
          AND pg_get_indexdef(indexrelid) =
            'CREATE INDEX v2_quotes_fixture_summary_idx ON public.v2_current_quotes USING btree (fixture_id, is_main DESC, source_updated_at DESC NULLS LAST, market_id, line, selection)'
    ) THEN
        RAISE EXCEPTION 'quote summary index definition or validity mismatch';
    END IF;
END $$;
