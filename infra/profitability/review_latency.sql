-- Use the message version and the attempt that produced the filled recommendation.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT :'begin'::timestamptz AS begin_at, :'end'::timestamptz AS end_at
), fills AS MATERIALIZED (
    SELECT o.signal_id, o.account_id, o.provider_identity_hash
    FROM v2_bet_orders o CROSS JOIN bounds b
    WHERE NOT o.manual AND o.provider_order_no IS NOT NULL
      AND o.provider_placed_at >= b.begin_at AND o.provider_placed_at < b.end_at
      AND b.end_at <= now()
), scope AS (
    SELECT count(*) AS fills, count(DISTINCT (account_id, provider_identity_hash)) AS bindings,
           count(*) FILTER (WHERE account_id IS NULL OR provider_identity_hash IS NULL) AS missing_identity
    FROM fills
), observations AS (
    SELECT s.phase, tg.event_kind, tg.source_version_at, tg.received_at,
           j.created_at AS job_at, request.request_at, s.created_at AS signal_at
    FROM (SELECT DISTINCT signal_id FROM fills) f JOIN v2_expert_signals s ON s.id=f.signal_id
    JOIN v2_expert_messages m ON m.id=s.message_id
    CROSS JOIN scope c
    LEFT JOIN LATERAL (
        SELECT q.evidence FROM v2_quote_evidence q
        WHERE q.reference_id=s.id AND q.evidence_kind='expert_signal' AND q.captured_at>=s.created_at
        ORDER BY q.captured_at,q.id LIMIT 1
    ) snapshot ON true
    LEFT JOIN v2_analysis_jobs j ON j.id=(CASE WHEN pg_input_is_valid(snapshot.evidence->>'job_id','uuid')
        THEN (snapshot.evidence->>'job_id')::uuid END) AND j.message_id=m.id
    LEFT JOIN LATERAL (
        SELECT min(t.created_at) AS request_at FROM v2_analysis_traces t
        WHERE t.job_id=j.id AND t.trace_kind='hermes_request'
          AND t.payload->>'attempt_id'=snapshot.evidence->>'attempt_id' AND t.payload->>'message_id'=m.id::text
    ) request ON true
    LEFT JOIN LATERAL (
        SELECT e.source_version_at,e.received_at,e.event_kind FROM v2_telegram_events e
        WHERE e.message_id=m.id AND e.source_id=m.source_id AND e.event_kind IN ('message','edit')
        ORDER BY e.received_at,e.id LIMIT 1
    ) tg ON true
    WHERE c.bindings=1 AND c.missing_identity=0
), verified AS (
    SELECT *, (source_version_at<=received_at AND received_at<=job_at
        AND job_at<=request_at AND request_at<=signal_at) IS TRUE AS ordered
    FROM observations
), coverage AS (
    SELECT CASE WHEN grouping(phase)=1 THEN 'all' ELSE phase END AS phase,
           count(*) AS signals, count(event_kind) AS with_event, count(job_at) AS with_job,
           count(request_at) AS with_request, count(*) FILTER (WHERE ordered) AS verified,
           count(*) FILTER (WHERE ordered AND event_kind='edit') AS verified_edits
    FROM verified GROUP BY GROUPING SETS ((), (phase))
), timings AS (
    SELECT CASE WHEN grouping(phase)=1 THEN 'all' ELSE phase END AS phase, stage,
           count(*) AS signals,
           round((percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM elapsed)))::numeric,2) AS median_seconds,
           round((percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM elapsed)))::numeric,2) AS p90_seconds
    FROM verified CROSS JOIN LATERAL (VALUES
        ('version_to_receive', received_at-source_version_at),
        ('receive_to_job', job_at-received_at),
        ('job_to_request', request_at-job_at),
        ('request_to_signal', signal_at-request_at)
    ) stages(stage, elapsed)
    WHERE ordered
    GROUP BY stage, GROUPING SETS ((), (phase))
)
SELECT jsonb_build_object('bounds',to_jsonb(b),'scope',to_jsonb(c),
    'coverage',(SELECT jsonb_agg(to_jsonb(v) ORDER BY phase) FROM coverage v),
    'timings',coalesce((SELECT jsonb_agg(to_jsonb(t) ORDER BY phase,stage) FROM timings t),'[]'::jsonb))
FROM bounds b CROSS JOIN scope c;
ROLLBACK;
