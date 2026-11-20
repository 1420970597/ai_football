-- Compare recommendation agreement visible before automatic sends.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT :'begin'::timestamptz AS begin_at, :'split'::timestamptz AS split_at,
           :'end'::timestamptz AS end_at, :'observed'::timestamptz AS observed_at
), fills AS MATERIALIZED (
    SELECT CASE WHEN o.provider_placed_at < b.split_at THEN 'early' ELSE 'late' END AS cohort,
           o.account_id, o.provider_identity_hash, s.fixture_id, s.phase, s.market_id, s.market_kind,
           s.period, s.line, s.selection, s.score_basis, s.score_baseline, s.expert_id, m.source_id,
           p.captured_at AS authorized_at, p.evidence->'score_baseline' AS permit_score,
           o.provider_stake AS stake, r.payload->>'currency' AS currency, r.payload->>'fees' IS NOT NULL AS has_fee,
           CASE WHEN pg_input_is_valid(r.payload->>'net_profit','numeric')
                THEN (r.payload->>'net_profit')::numeric END AS net
    FROM v2_bet_orders o CROSS JOIN bounds b
    LEFT JOIN v2_expert_signals s ON s.id=o.signal_id
    LEFT JOIN v2_expert_messages m ON m.id=s.message_id
    LEFT JOIN LATERAL (
        SELECT q.captured_at, q.evidence FROM v2_quote_evidence q
        WHERE q.reference_id=o.id AND q.evidence_kind='order_attempt' AND q.evidence->>'stage'='submission_authorized'
          AND q.evidence->>'attempt_id'=o.attempt_id::text AND q.evidence->>'signal_id'=s.id::text
          AND q.captured_at BETWEEN s.created_at AND o.provider_placed_at
        ORDER BY q.captured_at DESC,q.id DESC LIMIT 1
    ) p ON true
    LEFT JOIN LATERAL (
        SELECT e.payload FROM v2_order_receipt_evidence e
        WHERE e.order_id=o.id AND e.payload->>'order_no'=o.provider_order_no AND e.payload->>'state'='settled'
          AND e.observed_at >= o.provider_placed_at AND e.observed_at < b.observed_at
        ORDER BY e.observed_at DESC,e.id DESC LIMIT 1
    ) r ON true
    WHERE NOT o.manual AND o.provider_order_no IS NOT NULL
      AND o.provider_placed_at >= b.begin_at AND o.provider_placed_at < b.end_at
      AND b.begin_at < b.split_at AND b.split_at < b.end_at AND b.end_at <= b.observed_at AND b.observed_at <= now()
), coverage AS (
    SELECT count(*) AS fills, count(net) AS with_net, count(DISTINCT (account_id,provider_identity_hash)) AS bindings,
           count(*) FILTER (WHERE account_id IS NULL OR provider_identity_hash IS NULL) AS missing_binding,
           count(DISTINCT currency) AS known_currencies
    FROM fills
), comparable AS (
    SELECT *, CASE WHEN authorized_at IS NULL OR source_id IS NULL OR expert_id IS NULL THEN 'missing_context'
                   WHEN phase='live' AND (
                       score_baseline->>'home' ~ '^[0-9]+$' AND score_baseline->>'away' ~ '^[0-9]+$'
                       AND (score_baseline->>'home',score_baseline->>'away') = (permit_score->>'home',permit_score->>'away')
                       AND (period<>'second_half' OR (score_baseline->>'half_home' ~ '^[0-9]+$'
                                                    AND score_baseline->>'half_away' ~ '^[0-9]+$'))
                   ) IS NOT TRUE THEN 'unverified_live_score'
              END AS exclusion
    FROM fills CROSS JOIN coverage c
    WHERE c.bindings=1 AND c.missing_binding=0 AND c.known_currencies<=1
), compared AS (
    SELECT f.*, coalesce(f.exclusion, CASE WHEN x.same_direction AND x.other_direction THEN 'mixed'
                                           WHEN x.same_direction THEN 'same_direction'
                                           WHEN x.other_direction THEN 'other_direction'
                                           ELSE 'no_peer' END) AS evidence
    FROM comparable f
    LEFT JOIN LATERAL (
        SELECT bool_or(s.selection=f.selection) AS same_direction,
               bool_or(s.selection<>f.selection) AS other_direction
        FROM v2_expert_signals s JOIN v2_expert_messages m ON m.id=s.message_id
        WHERE f.exclusion IS NULL AND (s.fixture_id,s.phase,s.market_id,s.market_kind,s.period,s.score_basis)
            = (f.fixture_id,f.phase,f.market_id,f.market_kind,f.period,f.score_basis)
          AND s.line IS NOT DISTINCT FROM f.line AND s.expert_id<>f.expert_id AND m.source_id<>f.source_id
          AND s.created_at >= f.authorized_at - interval '5 minutes' AND s.created_at < f.authorized_at
          AND m.posted_at BETWEEN f.authorized_at - interval '5 minutes' AND s.created_at
          AND (s.withdrawn_at IS NULL OR s.withdrawn_at > f.authorized_at)
          AND (m.deleted_at IS NULL OR m.deleted_at > f.authorized_at)
          AND (f.phase='scheduled' OR (
               (s.score_baseline->>'home',s.score_baseline->>'away') = (f.score_baseline->>'home',f.score_baseline->>'away')
               AND (s.score_baseline->>'half_home',s.score_baseline->>'half_away')
                    IS NOT DISTINCT FROM (f.score_baseline->>'half_home',f.score_baseline->>'half_away')))
    ) x ON true
), results AS (
    SELECT cohort, CASE WHEN grouping(phase)=1 THEN 'all' ELSE phase END AS phase,
           CASE WHEN grouping(evidence)=1 THEN 'baseline' ELSE evidence END AS evidence,
           count(*) AS fills, count(net) AS settled, count(DISTINCT fixture_id) FILTER (WHERE net IS NOT NULL) AS settled_fixtures,
           count(*) FILTER (WHERE net IS NOT NULL AND (currency IS NULL OR NOT has_fee)) AS missing_cost_fields,
           sum(stake) FILTER (WHERE net IS NOT NULL) AS settled_stake, sum(net) AS receipt_net
    FROM compared GROUP BY cohort, GROUPING SETS ((), (phase), (evidence), (phase,evidence))
)
SELECT jsonb_build_object('bounds',to_jsonb(b),'coverage',to_jsonb(c),
    'results',coalesce((SELECT jsonb_agg(to_jsonb(s) ORDER BY cohort,phase,evidence) FROM results s),'[]'::jsonb))
FROM bounds b CROSS JOIN coverage c;
ROLLBACK;
