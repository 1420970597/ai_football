-- Compare pre-send freshness policies on the same set of automatic fills.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT :'begin'::timestamptz AS begin_at, :'split'::timestamptz AS split_at,
           :'end'::timestamptz AS end_at, :'observed'::timestamptz AS observed_at
), fills AS (
    SELECT o.account_id, o.provider_identity_hash, s.fixture_id, s.phase,
           CASE WHEN o.provider_placed_at < b.split_at THEN 'early' ELSE 'late' END AS period,
           o.provider_stake AS stake, receipt.payload->>'currency' AS currency,
           receipt.payload->>'fees' IS NOT NULL AS has_fee,
           CASE WHEN pg_input_is_valid(receipt.payload->>'net_profit', 'numeric')
                THEN (receipt.payload->>'net_profit')::numeric END AS net,
           (permit.captured_at >= s.created_at AND s.created_at >= m.posted_at
                AND permit.captured_at <= o.provider_placed_at) IS TRUE AS time_verified,
           permit.captured_at - s.created_at AS signal_age,
           permit.captured_at - m.posted_at AS message_age
    FROM v2_bet_orders o CROSS JOIN bounds b
    LEFT JOIN v2_expert_signals s ON s.id = o.signal_id
    LEFT JOIN v2_expert_messages m ON m.id = s.message_id
    LEFT JOIN LATERAL (
        SELECT q.captured_at
        FROM v2_quote_evidence q
        WHERE q.reference_id = o.id AND q.evidence_kind = 'order_attempt'
            AND q.evidence->>'stage' = 'submission_authorized'
            AND q.evidence->>'attempt_id' = o.attempt_id::text
            AND q.evidence->>'signal_id' = s.id::text
        ORDER BY q.captured_at DESC, q.id DESC LIMIT 1
    ) permit ON true
    LEFT JOIN LATERAL (
        SELECT e.payload
        FROM v2_order_receipt_evidence e
        WHERE e.order_id = o.id AND e.payload->>'order_no' = o.provider_order_no
            AND e.payload->>'state' = 'settled' AND e.observed_at < b.observed_at
        ORDER BY e.observed_at DESC, e.id DESC LIMIT 1
    ) receipt ON true
    WHERE NOT o.manual AND o.provider_order_no IS NOT NULL
      AND coalesce(o.provider_placed_at, o.created_at) >= b.begin_at
      AND coalesce(o.provider_placed_at, o.created_at) < b.end_at
      AND b.begin_at < b.split_at AND b.split_at < b.end_at AND b.end_at <= b.observed_at AND b.observed_at <= now()
), coverage AS (
    SELECT count(*) AS fills, count(net) AS with_net, count(*) FILTER (WHERE NOT time_verified) AS unverified_times,
           count(*) FILTER (WHERE account_id IS NULL OR provider_identity_hash IS NULL) AS unverified_binding,
           count(DISTINCT (account_id, provider_identity_hash)) AS account_bindings,
           count(DISTINCT currency) AS known_currencies
    FROM fills
), results AS (
    SELECT period, CASE WHEN grouping(phase) = 1 THEN 'all' ELSE phase END AS phase, rule,
           count(*) AS opportunities,
           count(*) FILTER (WHERE NOT time_verified) AS unverified_times,
           count(*) FILTER (WHERE keep) AS kept,
           count(net) FILTER (WHERE keep) AS kept_with_net,
           count(net) FILTER (WHERE NOT keep) AS removed_with_net,
           count(*) FILTER (WHERE keep AND net IS NOT NULL AND (currency IS NULL OR NOT has_fee)) AS without_cost_evidence,
           count(DISTINCT fixture_id) FILTER (WHERE keep AND net IS NOT NULL) AS settled_fixtures,
           sum(stake) FILTER (WHERE keep AND net IS NOT NULL) AS settled_stake,
           sum(net) FILTER (WHERE keep) AS receipt_net,
           round(avg(net / nullif(stake, 0)) FILTER (WHERE keep) * 100, 2) AS equal_stake_receipt_pct
    FROM fills CROSS JOIN coverage c
    CROSS JOIN LATERAL (VALUES
       ('baseline', true),
       ('signal_age_60s', time_verified AND signal_age <= interval '60 seconds'),
       ('message_age_120s', time_verified AND message_age <= interval '120 seconds')
    ) rules(rule, keep)
    WHERE c.account_bindings = 1 AND c.unverified_binding = 0 AND c.known_currencies <= 1
    GROUP BY period, rule, GROUPING SETS ((), (phase))
)
SELECT jsonb_build_object('bounds', to_jsonb(b), 'coverage', to_jsonb(c),
    'results', coalesce((SELECT jsonb_agg(to_jsonb(r) ORDER BY period, phase, rule) FROM results r), '[]'::jsonb))
FROM bounds b CROSS JOIN coverage c;
ROLLBACK;
