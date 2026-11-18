-- Count each message review job once, using its current attempt only.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT now() AS observed_at, :'begin'::timestamptz AS begin_at, :'end'::timestamptz AS end_at
), jobs AS MATERIALIZED (
    SELECT j.id,j.attempt_id,j.state,j.review_mode,j.last_error
    FROM v2_analysis_jobs j CROSS JOIN bounds b
    WHERE j.created_at>=b.begin_at AND j.created_at<b.end_at AND b.end_at<=b.observed_at
      AND b.begin_at<b.end_at AND b.end_at-b.begin_at<=interval '72 hours'
), observations AS (
    SELECT j.*, t.*, EXISTS (SELECT 1 FROM v2_review_fixture_bindings b
        WHERE b.job_id=j.id AND b.attempt_id=j.attempt_id) AS has_fixture,
        summary.payload AS receipt
    FROM jobs j LEFT JOIN LATERAL (
        SELECT bool_or(trace_kind='mcp_search_matches') AS searched,
               bool_or(trace_kind='mcp_list_match_markets') AS read_quotes,
               bool_or(trace_kind='mcp_record_expert_signals') AS attempted_record,
               bool_or(trace_kind='mcp_resolve_match' OR trace_kind='mcp_search_matches'
                   AND nullif(payload->'parameters'->>'team_a','') IS NOT NULL
                   AND nullif(payload->'parameters'->>'team_b','') IS NOT NULL) AS attempted_resolution,
               bool_or(trace_kind='mcp_list_match_markets' AND payload->>'status'='succeeded') AS market_read_succeeded,
               bool_or(trace_kind='mcp_list_match_markets' AND payload->>'status'='succeeded'
                   AND payload->'market_coverage'->>'matched_quotes' ~ '^[1-9][0-9]{0,5}$') AS match_found,
               bool_or(trace_kind='mcp_list_match_markets' AND payload->>'status'='succeeded'
                   AND (payload->'market_coverage'->>'matched_quotes' ~ '^[0-9]{1,6}$') IS NOT TRUE)
                   AS match_count_missing
        FROM v2_analysis_traces t
        WHERE t.job_id=j.id AND t.payload->>'attempt_id'=j.attempt_id::text
          AND trace_kind IN ('mcp_search_matches','mcp_resolve_match','mcp_list_match_markets','mcp_record_expert_signals')
    ) t ON true
    LEFT JOIN LATERAL (
        SELECT payload FROM v2_analysis_traces t
        WHERE t.job_id=j.id AND t.payload->>'attempt_id'=j.attempt_id::text
          AND trace_kind='system_review_result'
        ORDER BY sequence_no DESC LIMIT 1
    ) summary ON true
), outcomes AS (
    SELECT *, CASE
        WHEN state IN ('queued','running','retry') THEN 'in_progress'
        WHEN state='failed' AND last_error IN ('source_edited','source_deleted','raw_content_expired')
            THEN 'source_invalidated'
        WHEN state='failed' THEN 'job_failed'
        WHEN receipt IS NULL THEN 'no_current_attempt_receipt'
        WHEN receipt->>'accepted_count' ~ '^[1-9][0-9]{0,5}$' THEN 'recommendation_accepted'
        WHEN receipt->>'accepted_count' IS DISTINCT FROM '0' THEN 'invalid_receipt_count'
        WHEN attempted_record THEN 'record_attempt_without_acceptance'
        WHEN match_found THEN 'matching_quotes_without_record_attempt'
        WHEN match_count_missing THEN 'read_succeeded_match_count_unavailable'
        WHEN market_read_succeeded THEN 'read_succeeded_no_matching_quote'
        WHEN read_quotes THEN 'read_attempt_without_success'
        WHEN has_fixture THEN 'fixture_bound_without_market_read'
        WHEN searched THEN 'search_without_fixture_binding'
        ELSE 'no_search_or_market_read'
    END AS stage FROM observations
), counts AS (
    SELECT review_mode,state,stage,count(*) AS jobs,
           count(*) FILTER (WHERE receipt->>'completion_verified'='false') AS incomplete_reviews,
           count(*) FILTER (WHERE receipt->'completion_issues' ? 'text_intent_unverified') AS unverified_text_intent,
           count(*) FILTER (WHERE receipt->'completion_issues' ? 'visual_access_unverified') AS unverified_visual_access,
           count(*) FILTER (WHERE receipt->'completion_issues' ? 'source_total_review_gaps') AS uncovered_source_total
    FROM outcomes GROUP BY review_mode,state,stage
), search_evidence AS (
    SELECT j.review_mode, j.attempted_resolution IS TRUE AS attempted_resolution,
           j.receipt->'completion_issues' ? 'text_intent_unverified' AS text_intent_unverified,
           CASE WHEN s.nonempty THEN 'complete_nonempty'
                WHEN s.incomplete THEN 'pagination_incomplete'
                WHEN s.empty THEN 'complete_empty'
                ELSE 'search_rows_unavailable' END AS candidate_evidence
    FROM outcomes j CROSS JOIN LATERAL (
        SELECT bool_or(complete AND total_candidates>0) AS nonempty,
               bool_or(complete AND total_candidates=0) AS empty,
               bool_or(NOT complete) AS incomplete
        FROM v2_match_searches s WHERE s.job_id=j.id AND s.attempt_id=j.attempt_id
    ) s WHERE j.stage='search_without_fixture_binding'
), search_counts AS (
    SELECT review_mode,candidate_evidence,count(*) AS jobs,
           count(*) FILTER (WHERE attempted_resolution) AS attempted_resolution,
           count(*) FILTER (WHERE text_intent_unverified) AS unverified_text_intent
    FROM search_evidence GROUP BY review_mode,candidate_evidence
)
SELECT jsonb_build_object('bounds',to_jsonb(b),'groups',coalesce((SELECT jsonb_agg(to_jsonb(c)
    ORDER BY review_mode,state,stage) FROM counts c),'[]'::jsonb),
    'unbound_searches',coalesce((SELECT jsonb_agg(to_jsonb(s) ORDER BY review_mode,candidate_evidence)
        FROM search_counts s),'[]'::jsonb)) FROM bounds b;
ROLLBACK;
