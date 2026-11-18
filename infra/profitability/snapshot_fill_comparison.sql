-- Compare natural fills to the retained recommendation-time price set.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT :'begin'::timestamptz AS begin_at, now() AS observed_at
), fills AS (
    SELECT f.sport,s.phase,s.market_kind,s.period,o.state,o.provider_price AS fill_price,
           p.price AS preflight_price,s.recommendation_price,
           e.id IS NOT NULL AS has_snapshot, p.id IS NOT NULL AS has_preflight,
           p.fixture_id = s.fixture_id AND p.market_id = s.market_id AND p.period = s.period
             AND p.line IS NOT DISTINCT FROM s.line AND p.selection = s.selection
             AND p.evidence->>'phase' = s.phase AND p.evidence->>'market_kind' = s.market_kind
             AND p.evidence->>'score_basis' = s.score_basis
             AND p.evidence->>'market_instance_id' = snapshot.data->>'market_instance_id'
             AND p.evidence->>'play_id' = snapshot.data->>'play_id'
             AND p.evidence->>'selection_id' = options.selected_id
             AND (s.phase <> 'live' OR (
                 s.score_baseline->>'home' IS NOT NULL AND s.score_baseline->>'away' IS NOT NULL
                 AND s.score_baseline->>'home' = p.evidence->'score_baseline'->>'home'
                 AND s.score_baseline->>'away' = p.evidence->'score_baseline'->>'away'
                 AND concat(s.score_baseline->>'home', ':', s.score_baseline->>'away') = p.evidence->>'score_benchmark'
                 AND (s.period = 'full_time' OR (
                       -- Native market 18 settles on the entire first half.
                       s.period = 'first_half' AND f.sport = 'football' AND s.fixture_id ~ '^leyu:[0-9]+$'
                       AND s.market_id = '18' AND s.market_kind = 'total'
                       AND s.score_basis IN ('unknown', 'full_period')
                 ))
             )) AS same_snapshot_contract,
           options.selected_price = s.recommendation_price
             AND options.sides = CASE
                 WHEN s.market_kind = 'winner' AND f.sport = 'football' THEN ARRAY['away','draw','home']
                 WHEN s.market_kind IN ('winner','handicap') THEN ARRAY['away','home']
                 WHEN s.market_kind IN ('total','team_total') THEN ARRAY['over','under'] END
             AND options.valid_prices = cardinality(options.sides)
             AND options.unique_ids = cardinality(options.sides) AS valid_options,
           options.inverse_sum,
           o.settled_net, o.provider_fees, o.provider_currency, o.account_id, o.provider_identity_hash
    FROM v2_bet_orders o JOIN v2_expert_signals s ON s.id = o.signal_id
    LEFT JOIN v2_fixtures f ON f.id = s.fixture_id CROSS JOIN bounds b
    LEFT JOIN LATERAL (
        SELECT q.id, q.evidence
        FROM v2_quote_evidence q
        WHERE q.reference_id = s.id AND q.evidence_kind = 'expert_signal' AND q.evidence ? 'market_snapshot'
          AND q.fixture_id = s.fixture_id AND q.market_id = s.market_id AND q.period = s.period
          AND q.line IS NOT DISTINCT FROM s.line AND q.selection = s.selection AND q.price = s.recommendation_price
          AND q.source_updated_at = s.quote_time AND q.score_baseline IS NOT DISTINCT FROM s.score_baseline
          AND q.captured_at <= o.provider_placed_at
        ORDER BY q.captured_at, q.id LIMIT 1
    ) e ON true
    CROSS JOIN LATERAL (SELECT e.evidence->'market_snapshot' AS data) snapshot
    LEFT JOIN LATERAL (
        SELECT array_agg(item->>'selection' ORDER BY item->>'selection') AS sides,
               count(*) FILTER (WHERE price > 1 AND price < 10000000) AS valid_prices,
               count(DISTINCT nullif(item->>'selection_id','')) AS unique_ids,
               min(item->>'selection_id') FILTER (WHERE item->>'selection' = s.selection) AS selected_id,
               min(price) FILTER (WHERE item->>'selection' = s.selection) AS selected_price,
               sum(1 / price) FILTER (WHERE price > 1 AND price < 10000000) AS inverse_sum
        FROM jsonb_array_elements(CASE WHEN jsonb_typeof(snapshot.data->'options') = 'array'
              THEN snapshot.data->'options' ELSE '[]'::jsonb END) AS quote(item)
        CROSS JOIN LATERAL (SELECT CASE WHEN pg_input_is_valid(item->>'price','numeric')
              THEN (item->>'price')::numeric END AS price) parsed
    ) options ON e.id IS NOT NULL
    LEFT JOIN LATERAL (
        SELECT q.id,q.fixture_id,q.market_id,q.period,q.line,q.selection,q.price,q.evidence
        FROM v2_quote_evidence q
        WHERE q.reference_id = o.id AND q.evidence_kind = 'order_attempt'
          AND q.evidence->>'stage' = 'preflight_validated' AND q.evidence->>'attempt_id' = o.attempt_id::text
          AND q.evidence->>'signal_id' = s.id::text
          AND q.captured_at BETWEEN s.created_at AND o.provider_placed_at
        ORDER BY q.captured_at DESC, q.id DESC LIMIT 1
    ) p ON true
    WHERE s.created_at >= b.begin_at AND s.created_at < b.observed_at
      AND o.provider_order_no IS NOT NULL AND o.state IN ('accepted','settled')
), compared AS (
    SELECT *, (same_snapshot_contract AND valid_options AND fill_price > 1 AND preflight_price > 1) IS TRUE AS comparable
    FROM fills
), summary AS (
    SELECT sport,phase,market_kind,period,grouping(sport,phase,market_kind,period)=15 AS all_markets,
           count(*) AS fills,count(*) FILTER(WHERE state='settled') AS settled,
           count(*) FILTER(WHERE has_snapshot) AS with_snapshot,
           count(*) FILTER(WHERE has_snapshot AND has_preflight) AS with_snapshot_and_preflight,
           count(*) FILTER(WHERE comparable) AS comparable_prices,
           count(*) FILTER(WHERE comparable AND fill_price < recommendation_price) AS below_recommendation,
           count(*) FILTER(WHERE comparable AND fill_price = recommendation_price) AS equal_recommendation,
           count(*) FILTER(WHERE comparable AND fill_price > recommendation_price) AS above_recommendation,
           count(*) FILTER(WHERE comparable AND fill_price < preflight_price) AS below_preflight,
           count(*) FILTER(WHERE comparable AND fill_price = preflight_price) AS equal_preflight,
           count(*) FILTER(WHERE comparable AND fill_price > preflight_price) AS above_preflight,
           count(*) FILTER(WHERE comparable AND fill_price >= recommendation_price * inverse_sum)
               AS reaches_normalized_reference,
           round(avg(inverse_sum) FILTER(WHERE comparable),5) AS mean_inverse_sum,
           round(avg(fill_price / (recommendation_price * inverse_sum) - 1) FILTER(WHERE comparable) * 100,2)
               AS mean_distance_to_reference_percent,
           CASE WHEN count(DISTINCT (account_id, provider_identity_hash)) = 1
                 AND bool_and(account_id IS NOT NULL AND provider_identity_hash IS NOT NULL)
                 AND count(DISTINCT provider_currency) <= 1
                THEN sum(settled_net) FILTER(WHERE comparable AND state='settled') END AS comparable_receipt_net,
           count(*) FILTER(WHERE comparable AND state='settled' AND (provider_fees IS NULL OR provider_currency IS NULL))
               AS comparable_missing_cost_fields
    FROM compared
    GROUP BY GROUPING SETS ((), (sport,phase,market_kind,period))
)
SELECT jsonb_build_object('bounds',to_jsonb(b),'comparison',(
    SELECT jsonb_agg(to_jsonb(s) ORDER BY all_markets DESC,sport,phase,market_kind,period) FROM summary s
)) FROM bounds b;
ROLLBACK;
