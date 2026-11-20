-- Include real-time reviews regardless of whether they produced a recommendation or order.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT :'begin'::timestamptz AS begin_at, :'end'::timestamptz AS end_at
), reads AS MATERIALIZED (
    SELECT t.id, t.job_id, t.sequence_no, t.created_at, t.payload->>'attempt_id' AS attempt_id,
           nullif(t.payload->>'fixture_id','') AS fixture_id,
           CASE WHEN jsonb_typeof(t.payload->'market_filter')='object'
                THEN t.payload->'market_filter' END AS market_filter,
           t.payload->>'status' AS status, t.payload->'refresh'->>'reason' AS reason,
           CASE WHEN jsonb_typeof(t.payload->'elapsed_ms')='number'
                THEN (t.payload->>'elapsed_ms')::numeric END AS elapsed_ms
    FROM v2_analysis_traces t JOIN v2_analysis_jobs j ON j.id=t.job_id
    CROSS JOIN bounds b
    WHERE j.review_mode='real_time' AND t.trace_kind='mcp_list_match_markets'
      AND t.created_at>=b.begin_at AND t.created_at<b.end_at AND b.end_at<=now()
), outcomes AS (
    SELECT status, reason, count(*) AS calls,
           count(*) FILTER (WHERE elapsed_ms>=0) AS timed_calls,
           round((percentile_cont(0.9) WITHIN GROUP (ORDER BY elapsed_ms)
               FILTER (WHERE elapsed_ms>=0))::numeric,2) AS p90_ms
    FROM reads GROUP BY status,reason
), refresh_writes AS (
    SELECT market_filter, created_at-lag(created_at) OVER (PARTITION BY fixture_id,market_filter
        ORDER BY created_at,id) AS elapsed_since_same_filter
    FROM reads WHERE status='succeeded' AND reason='written' AND fixture_id IS NOT NULL
), repeat_counts AS (
    SELECT count(*) FILTER (WHERE market_filter IS NOT NULL) AS writes_with_fixture_and_filter,
           count(*) FILTER (WHERE market_filter IS NOT NULL AND elapsed_since_same_filter<=interval '10 seconds')
               AS same_filter_within_10s,
           count(*) FILTER (WHERE market_filter IS NOT NULL AND elapsed_since_same_filter<=interval '30 seconds')
               AS same_filter_within_30s
    FROM refresh_writes
), unmatched AS (
    SELECT r.market_filter->>'market_kind' AS market_kind, r.market_filter->>'period' AS period,
           count(*) AS calls, count(*) FILTER (WHERE EXISTS (
               SELECT 1 FROM v2_analysis_traces t
               WHERE t.job_id=r.job_id AND t.payload->>'attempt_id'=r.attempt_id
                 AND t.sequence_no>r.sequence_no AND t.created_at<b.end_at
                 AND t.trace_kind='mcp_record_expert_signals'
                 AND jsonb_typeof(t.payload->'accepted')='array' AND t.payload->'accepted'<>'[]'::jsonb
           )) AS later_acceptance_on_attempt
    FROM reads r CROSS JOIN bounds b
    WHERE r.status='succeeded' AND r.reason='no_quotes_for_filter'
    GROUP BY 1,2
)
SELECT jsonb_build_object('bounds',to_jsonb(b),
    'calls',(SELECT count(*) FROM reads),
    'outcomes',coalesce((SELECT jsonb_agg(to_jsonb(o) ORDER BY calls DESC,status,reason) FROM outcomes o),'[]'::jsonb),
    'repeat_writes',(SELECT to_jsonb(r) FROM repeat_counts r),
    'unmatched_filters',coalesce((SELECT jsonb_agg(to_jsonb(u) ORDER BY calls DESC,market_kind,period)
        FROM unmatched u),'[]'::jsonb))
FROM bounds b;
ROLLBACK;
