-- Replay limits on simultaneously open filled orders within a fixture.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH RECURSIVE bounds AS (
    SELECT :'begin'::timestamptz AS begin_at, :'split'::timestamptz AS split_at,
           :'end'::timestamptz AS end_at, :'observed'::timestamptz AS observed_at
), fills AS MATERIALIZED (
    SELECT o.id, o.account_id, o.provider_identity_hash, s.fixture_id, s.phase, o.manual,
           o.provider_placed_at AS placed_at, o.provider_stake AS stake,
           first_receipt.observed_at AS closed_at, confirmation.observed_at AS accepted_at,
           CASE WHEN pg_input_is_valid(latest.payload->>'net_profit', 'numeric')
                THEN (latest.payload->>'net_profit')::numeric END AS net,
           latest.payload->>'currency' AS currency, latest.payload->>'fees' IS NOT NULL AS has_fee
    FROM v2_bet_orders o
    LEFT JOIN v2_expert_signals s ON s.id = o.signal_id
    CROSS JOIN bounds b
    LEFT JOIN LATERAL (
        SELECT min(e.observed_at) FILTER (WHERE e.payload->>'state' = 'settled') AS observed_at
        FROM v2_order_receipt_evidence e
        WHERE e.order_id = o.id AND e.observed_at <= b.observed_at
          AND e.payload->>'order_no' = o.provider_order_no
    ) first_receipt ON true
    LEFT JOIN LATERAL (
        SELECT min(e.observed_at) AS observed_at FROM v2_order_receipt_evidence e
        WHERE e.order_id = o.id AND e.observed_at <= b.observed_at
          AND e.payload->>'order_no' = o.provider_order_no
          AND e.payload->>'state' IN ('accepted', 'unsettled', 'settled')
    ) confirmation ON true
    LEFT JOIN LATERAL (
        SELECT e.payload
        FROM v2_order_receipt_evidence e
        WHERE e.order_id = o.id AND e.observed_at <= b.observed_at
          AND e.payload->>'order_no' = o.provider_order_no AND e.payload->>'state' = 'settled'
        ORDER BY e.observed_at DESC, e.id DESC LIMIT 1
    ) latest ON true
    WHERE o.provider_order_no IS NOT NULL
      AND coalesce(o.provider_placed_at, o.created_at) < b.end_at
      AND (coalesce(o.provider_placed_at, o.created_at) >= b.begin_at
           OR first_receipt.observed_at >= b.begin_at OR first_receipt.observed_at IS NULL)
      AND b.begin_at < b.split_at AND b.split_at < b.end_at AND b.end_at <= b.observed_at AND b.observed_at <= now()
), coverage AS (
    SELECT count(*) FILTER (WHERE placed_at >= begin_at) AS fills_in_period,
           count(*) FILTER (WHERE fixture_id IS NULL OR account_id IS NULL OR provider_identity_hash IS NULL
                               OR placed_at IS NULL OR accepted_at IS NULL OR stake IS NULL OR stake <= 0
                               OR closed_at < placed_at) AS invalid_fills,
           count(DISTINCT (account_id, provider_identity_hash)) AS account_bindings,
           count(DISTINCT currency) AS known_currencies
    FROM fills CROSS JOIN bounds
), sequence AS MATERIALIZED (
    SELECT f.*, row_number() OVER (PARTITION BY fixture_id ORDER BY placed_at, accepted_at, id) AS step,
           count(*) OVER (PARTITION BY fixture_id, placed_at, accepted_at) AS tied
    FROM fills f CROSS JOIN coverage c
    WHERE c.invalid_fills = 0 AND c.account_bindings = 1 AND c.known_currencies <= 1
), policies (cap) AS (
    VALUES (0), (1), (2)
), replay AS (
    SELECT f.fixture_id, p.cap, 0::bigint AS step, ARRAY[]::timestamptz[] AS open_until, true AS retained
    FROM (SELECT DISTINCT fixture_id FROM sequence) f CROSS JOIN policies p
    UNION ALL
    SELECT r.fixture_id, r.cap, s.step,
           CASE WHEN decision.retained THEN array_append(active.deadlines, coalesce(s.closed_at, 'infinity'))
                ELSE active.deadlines END,
           decision.retained
    FROM replay r JOIN sequence s ON s.fixture_id = r.fixture_id AND s.step = r.step + 1
    CROSS JOIN bounds b
    CROSS JOIN LATERAL (
        SELECT ARRAY(SELECT x FROM unnest(r.open_until) AS x WHERE x >= s.placed_at) AS deadlines
    ) active
    CROSS JOIN LATERAL (
        SELECT r.cap = 0 OR s.manual OR s.placed_at < b.begin_at OR cardinality(active.deadlines) < r.cap AS retained
    ) decision
), comparison AS (
    SELECT CASE WHEN s.placed_at < b.split_at THEN 'early' ELSE 'late' END AS period,
           r.cap, count(*) AS opportunities,
           count(*) FILTER (WHERE s.tied > 1) AS ambiguous_sequence,
           count(*) FILTER (WHERE NOT r.retained) AS removed,
           count(DISTINCT s.fixture_id) FILTER (WHERE NOT r.retained) AS fixtures_with_removals,
           count(*) FILTER (WHERE r.retained AND s.net IS NOT NULL) AS retained_with_net,
           count(*) FILTER (WHERE NOT r.retained AND s.net IS NULL) AS removed_without_net,
           count(*) FILTER (WHERE s.net IS NOT NULL AND (s.currency IS NULL OR NOT s.has_fee)) AS net_without_cost_evidence,
           sum(s.stake) FILTER (WHERE r.retained AND s.net IS NOT NULL) AS retained_settled_stake,
           sum(s.net) FILTER (WHERE r.retained) AS retained_receipt_net,
           sum(s.net) FILTER (WHERE NOT r.retained) AS removed_receipt_net
    FROM replay r JOIN sequence s USING (fixture_id, step) CROSS JOIN bounds b
    WHERE s.placed_at >= b.begin_at
    GROUP BY 1, 2
), fixture_receipts AS (
    SELECT CASE WHEN s.placed_at < b.split_at THEN 'early' ELSE 'late' END AS period,
           CASE WHEN grouping(s.phase) = 1 THEN 'all' ELSE coalesce(s.phase, 'unknown') END AS phase,
           s.fixture_id, count(*) AS fills, count(s.net) AS fills_with_net, sum(s.net) AS net
    FROM sequence s CROSS JOIN bounds b
    WHERE s.placed_at >= b.begin_at
    GROUP BY 1, s.fixture_id, GROUPING SETS ((), (s.phase))
), fixture_sensitivity AS (
    SELECT period, phase, count(*) AS fixtures, count(net) AS fixtures_with_net,
           count(*) FILTER (WHERE fills_with_net < fills) AS fixtures_missing_any_net,
           count(*) FILTER (WHERE net < 0) AS negative_fixtures,
           count(*) FILTER (WHERE net > 0) AS positive_fixtures,
           sum(net) AS receipt_net,
           CASE WHEN count(net) > 1 THEN sum(net) - max(net) END AS lowest_net_excluding_one,
           CASE WHEN count(net) > 1 THEN sum(net) - min(net) END AS highest_net_excluding_one
    FROM fixture_receipts
    GROUP BY period, phase
)
SELECT jsonb_build_object('bounds', to_jsonb(b), 'coverage', to_jsonb(c),
    'comparisons', coalesce((SELECT jsonb_agg(to_jsonb(p) ORDER BY period, cap) FROM comparison p), '[]'::jsonb),
    'fixture_sensitivity', coalesce((SELECT jsonb_agg(to_jsonb(f) ORDER BY period, phase)
        FROM fixture_sensitivity f), '[]'::jsonb))
FROM bounds b CROSS JOIN coverage c;
ROLLBACK;
