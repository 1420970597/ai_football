-- Expand only the review-search contract; fixtures, quotes and signals remain unchanged.
ALTER TABLE v2_match_searches DROP CONSTRAINT v2_match_searches_phase_check;
ALTER TABLE v2_match_searches ADD CONSTRAINT v2_match_searches_phase_check
    CHECK (phase IN ('all', 'live', 'scheduled', 'finished', 'cancelled'));
