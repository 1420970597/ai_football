-- Closing complete/terminal snapshots must not scan historical closed quotes.
-- Include suspended options: an active-only index cannot serve this predicate.
CREATE INDEX v2_quotes_nonclosed_fixture_idx
    ON v2_current_quotes(fixture_id) WHERE status <> 'closed';
