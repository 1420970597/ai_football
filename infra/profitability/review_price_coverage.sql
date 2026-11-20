-- Recover complete option sets only from the same retained review read.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT now() AS observed_at
), signals AS MATERIALIZED (
    SELECT s.id, s.phase, s.market_kind, s.market_id, s.period, f.sport, s.fixture_id,
           s.line, s.selection, s.score_basis, s.score_baseline,
           s.recommendation_price, s.quote_time, s.created_at, ev.evidence,
           s.market_kind IN ('handicap','total','team_total')
               AND abs(s.line)%1 IN (0,0.25,0.75) AS requires_adjacent_lines,
           CASE WHEN s.market_kind = 'winner' AND f.sport = 'football' THEN ARRAY['away','draw','home']
                WHEN s.market_kind IN ('winner','handicap') THEN ARRAY['away','home']
                ELSE ARRAY['over','under'] END AS expected_sides
    FROM v2_expert_signals s JOIN v2_fixtures f ON f.id = s.fixture_id CROSS JOIN bounds b
    LEFT JOIN LATERAL (
        SELECT q.evidence
        FROM v2_quote_evidence q
        WHERE q.reference_id = s.id AND q.evidence_kind = 'expert_signal'
          AND q.fixture_id = s.fixture_id AND q.market_id = s.market_id AND q.period = s.period
          AND q.selection = s.selection AND coalesce(q.line, 0) = coalesce(s.line, 0)
          AND q.price = s.recommendation_price AND q.source_updated_at = s.quote_time
          AND q.score_baseline IS NOT DISTINCT FROM s.score_baseline
          AND q.evidence->>'score_basis' = s.score_basis AND q.evidence->>'market_kind' = s.market_kind
        ORDER BY q.captured_at, q.id LIMIT 1
    ) ev ON true
    WHERE s.created_at >= b.observed_at - interval '72 hours' AND s.created_at < b.observed_at
      AND s.withdrawn_at IS NULL AND s.market_kind IN ('winner','handicap','total','team_total')
      AND f.sport IN ('football','basketball')
), candidates AS (
    SELECT s.*, left_read.quote_id IS NOT NULL AS selected_match,
        market.same_read_sides = s.expected_sides
            AND market.same_read_option_ids = cardinality(s.expected_sides) AS same_read_complete,
        market.same_source_sides = s.expected_sides
            AND market.same_source_option_ids = cardinality(s.expected_sides) AS same_source_complete,
        market.same_source_implied_sum,
        adjacent.lines_seen = 2 AS both_adjacent_lines_seen,
        adjacent.unique_complete_lines = 2 AS both_adjacent_lines_complete,
        adjacent.unique_same_time_lines = 2 AS both_adjacent_lines_at_quote_time
    FROM signals s
    LEFT JOIN v2_review_market_reads left_read
        ON left_read.job_id = CASE WHEN pg_input_is_valid(s.evidence->>'job_id','uuid') THEN (s.evidence->>'job_id')::uuid END
        AND left_read.attempt_id = CASE WHEN pg_input_is_valid(s.evidence->>'attempt_id','uuid') THEN (s.evidence->>'attempt_id')::uuid END
        AND left_read.quote_id = CASE WHEN pg_input_is_valid(s.evidence->>'quote_id','uuid') THEN (s.evidence->>'quote_id')::uuid END
        AND left_read.created_at <= s.created_at AND left_read.source_updated_at <= left_read.created_at
        AND left_read.source_updated_at = s.quote_time AND left_read.price = s.recommendation_price
        AND left_read.quote_context->>'fixture_id' = s.fixture_id
        AND left_read.quote_context->>'market_id' = s.market_id AND left_read.quote_context->>'period' = s.period
        AND left_read.quote_context->>'phase' = s.phase AND left_read.quote_context->>'market_kind' = s.market_kind
        AND left_read.quote_context->>'selection' = s.selection
        AND left_read.quote_context->>'score_basis' = s.score_basis
        AND jsonb_build_array(left_read.quote_context->'home_score', left_read.quote_context->'away_score',
            left_read.quote_context->'half_home_score', left_read.quote_context->'half_away_score')
            = jsonb_build_array(s.score_baseline->'home',s.score_baseline->'away',
                s.score_baseline->'half_home',s.score_baseline->'half_away')
        AND (CASE WHEN pg_input_is_valid(left_read.quote_context->>'line','numeric')
                  THEN (left_read.quote_context->>'line')::numeric END) IS NOT DISTINCT FROM s.line
        AND (s.line IS NOT NULL OR left_read.quote_context->>'line' IS NULL)
        AND nullif(left_read.quote_context->>'market_instance_id','') IS NOT NULL
        AND nullif(left_read.quote_context->>'play_id','') IS NOT NULL
    LEFT JOIN LATERAL (
        SELECT array_agg(side ORDER BY side) AS same_read_sides,
               count(DISTINCT q.quote_context->>'selection_id') AS same_read_option_ids,
               array_agg(side ORDER BY side) FILTER (WHERE q.source_updated_at = s.quote_time) AS same_source_sides,
               count(DISTINCT q.quote_context->>'selection_id') FILTER (WHERE q.source_updated_at = s.quote_time)
                   AS same_source_option_ids,
               sum(1 / q.price) FILTER (WHERE q.source_updated_at = s.quote_time) AS same_source_implied_sum
        FROM v2_review_market_reads q
        CROSS JOIN LATERAL (SELECT q.quote_context->>'selection' AS side) AS option
        WHERE q.job_id = left_read.job_id AND q.attempt_id = left_read.attempt_id
          AND q.created_at = left_read.created_at AND q.source_updated_at <= q.created_at AND q.price > 1
          AND side = ANY(s.expected_sides) AND nullif(q.quote_context->>'selection_id','') IS NOT NULL
          AND q.quote_context - ARRAY['selection','selection_id','current_confirmation']
              = left_read.quote_context - ARRAY['selection','selection_id','current_confirmation']
    ) market ON left_read.quote_id IS NOT NULL
    LEFT JOIN LATERAL (
        SELECT count(*) AS lines_seen,
               count(*) FILTER (WHERE by_line.complete_instances=1) AS unique_complete_lines,
               count(*) FILTER (WHERE by_line.complete_instances=1 AND by_line.same_time_instances=1)
                   AS unique_same_time_lines
        FROM (
            SELECT by_instance.line,
                   count(*) FILTER (WHERE by_instance.sides=s.expected_sides AND by_instance.option_ids=2)
                       AS complete_instances,
                   count(*) FILTER (WHERE by_instance.same_time_sides=s.expected_sides
                       AND by_instance.same_time_option_ids=2) AS same_time_instances
            FROM (
                SELECT parsed.line,q.quote_context->>'market_instance_id' AS instance_id,
                       array_agg(parsed.side ORDER BY parsed.side) AS sides,
                       count(DISTINCT q.quote_context->>'selection_id') AS option_ids,
                       array_agg(parsed.side ORDER BY parsed.side) FILTER (WHERE q.source_updated_at=s.quote_time)
                           AS same_time_sides,
                       count(DISTINCT q.quote_context->>'selection_id') FILTER (WHERE q.source_updated_at=s.quote_time)
                           AS same_time_option_ids
                FROM v2_review_market_reads q CROSS JOIN LATERAL (
                    SELECT CASE WHEN pg_input_is_valid(q.quote_context->>'line','numeric')
                           THEN (q.quote_context->>'line')::numeric END AS line,
                           q.quote_context->>'selection' AS side
                ) parsed
                WHERE s.requires_adjacent_lines AND q.job_id=left_read.job_id AND q.attempt_id=left_read.attempt_id
                  AND q.created_at=left_read.created_at AND q.source_updated_at<=q.created_at
                  AND q.price>1 AND q.price<'Infinity'::numeric
                  AND parsed.line IN (floor(s.line+0.5)-0.5,floor(s.line+0.5)+0.5)
                  AND parsed.side=ANY(s.expected_sides) AND nullif(q.quote_context->>'selection_id','') IS NOT NULL
                  AND nullif(q.quote_context->>'market_instance_id','') IS NOT NULL
                  AND q.quote_context - ARRAY['line','market_instance_id','selection','selection_id','current_confirmation']
                      = left_read.quote_context - ARRAY['line','market_instance_id','selection','selection_id','current_confirmation']
                GROUP BY parsed.line,instance_id
            ) by_instance GROUP BY by_instance.line
        ) by_line
    ) adjacent ON left_read.quote_id IS NOT NULL AND s.requires_adjacent_lines
), summary AS (
    SELECT sport, phase, market_kind, period, grouping(sport,phase,market_kind,period) = 15 AS all_markets,
           count(*) AS recommendations, count(*) FILTER (WHERE evidence IS NOT NULL) AS saved_evidence,
           count(*) FILTER (WHERE selected_match) AS matching_read,
           count(*) FILTER (WHERE same_read_complete) AS complete_in_read,
           count(*) FILTER (WHERE same_source_complete) AS complete_at_same_source_time,
           round(avg(same_source_implied_sum) FILTER (WHERE same_source_complete), 5) AS mean_implied_sum,
           count(*) FILTER (WHERE requires_adjacent_lines) AS adjacent_demand,
           count(*) FILTER (WHERE both_adjacent_lines_seen) AS adjacent_lines_seen,
           count(*) FILTER (WHERE both_adjacent_lines_complete) AS adjacent_complete_in_read,
           count(*) FILTER (WHERE both_adjacent_lines_at_quote_time) AS adjacent_complete_at_quote_time
    FROM candidates
    GROUP BY GROUPING SETS ((), (sport,phase,market_kind,period))
)
SELECT jsonb_build_object('observed_at', b.observed_at,
    'coverage', (SELECT jsonb_agg(to_jsonb(s) ORDER BY all_markets DESC, sport, phase, market_kind, period) FROM summary s))
FROM bounds b;
ROLLBACK;
