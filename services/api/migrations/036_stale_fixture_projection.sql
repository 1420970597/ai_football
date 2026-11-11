-- A live or scheduled projection the source stopped reporting must not keep
-- masquerading as current. Measured before this migration: of 1631 `live` rows
-- only 184 had kicked off within the previous three hours, while 941 had kicked
-- off more than a day earlier; the public list returned all of them as in play
-- and the review tools offered them as tradable. 工程约束 §十二.3 forbids exactly
-- that: a missing upstream row may not be preserved as if it were tradable.
--
-- received_at cannot serve as the presence signal. The fixture upsert only
-- rewrites a row when the content actually changed, so a row that is still
-- reported but unchanged keeps an old timestamp. Measured: 13 future scheduled
-- rows had been silent for more than six hours purely because nothing about them
-- changes between polls. last_reported_at is written by ingestion for every row
-- the accepted snapshot still contains, throttled, and is a lower bound on the
-- last time the source still listed the row. It records presence only: no
-- result, score, cancellation or kickoff correction is implied.
--
-- Ingestion clears stale_projection_at as soon as the source reports the row
-- again, so a delayed, resumed or corrected match returns on its own.
--
-- Rollback: DROP INDEX IF EXISTS v2_fixtures_reported_recently_idx;
--           ALTER TABLE v2_fixtures DROP COLUMN stale_projection_at;
--           ALTER TABLE v2_fixtures DROP COLUMN last_reported_at;
ALTER TABLE v2_fixtures ADD COLUMN IF NOT EXISTS last_reported_at TIMESTAMPTZ;
ALTER TABLE v2_fixtures ADD COLUMN IF NOT EXISTS stale_projection_at TIMESTAMPTZ;

-- Seed optimistically at migration time. Backfilling from received_at would
-- retire rows that are still reported but unchanged, which for a scheduled
-- fixture is every row with no score and no clock. Nothing is retired here: the
-- sweeper starts counting from this instant, so an already-silent phantom is
-- gone one horizon later with no chance of hiding a genuinely live match.
UPDATE v2_fixtures SET last_reported_at = clock_timestamp()
WHERE last_reported_at IS NULL AND decision_context_only_at IS NULL;

-- The sweeper looks only at rows that are still current and still open, which
-- is a few thousand rows out of sixteen thousand. Keeping that set in its own
-- partial index makes the two-second sweep an index range read instead of a
-- repeated sequential scan.
CREATE INDEX IF NOT EXISTS v2_fixtures_reported_recently_idx ON v2_fixtures(last_reported_at)
    WHERE stale_projection_at IS NULL AND decision_context_only_at IS NULL
      AND phase IN ('live', 'scheduled');

COMMENT ON COLUMN v2_fixtures.last_reported_at IS
    'Lower bound on the last accepted snapshot that still contained this source row; not a quote or match time.';
COMMENT ON COLUMN v2_fixtures.stale_projection_at IS
    'Source stopped reporting this live/scheduled row; excluded from current lists until reported again.';
