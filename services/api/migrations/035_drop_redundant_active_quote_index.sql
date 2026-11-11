-- v2_current_quotes_active_idx(fixture_id, source_updated_at DESC) WHERE status='active'
-- has the same key as v2_current_quotes_fixture_fresh_idx, which covers every
-- status. No query filters active quotes by fixture in that order, so each
-- active quote update maintains it for nothing.
--
-- This runner applies migrations inside one transaction, so the drop cannot be
-- CONCURRENTLY here. Production may run
--   DROP INDEX CONCURRENTLY IF EXISTS v2_current_quotes_active_idx;
-- beforehand; this file then only records the version.
--
-- Rollback: CREATE INDEX CONCURRENTLY v2_current_quotes_active_idx
--   ON v2_current_quotes(fixture_id, source_updated_at DESC) WHERE status='active';
-- Returning to a release without this file also needs its row removed from
-- v2_schema_migrations, because schema verification compares the exact set.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_index
        WHERE indexrelid = to_regclass('public.v2_current_quotes_fixture_fresh_idx')
          AND indisvalid AND indisready
          AND pg_get_indexdef(indexrelid) =
            'CREATE INDEX v2_current_quotes_fixture_fresh_idx ON public.v2_current_quotes USING btree (fixture_id, source_updated_at DESC)'
    ) THEN
        RAISE EXCEPTION 'covering fixture quote index is missing or invalid';
    END IF;
END $$;
DROP INDEX IF EXISTS v2_current_quotes_active_idx;
