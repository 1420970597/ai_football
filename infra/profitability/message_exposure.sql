-- Placement cohorts for accepted automatic orders, with settlement evidence as of a fixed cutoff.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT :'begin'::timestamptz AS begin_at, :'end'::timestamptz AS end_at,
           :'observed'::timestamptz AS observed_at, :'anchor'::timestamptz AS anchor
), fills AS MATERIALIZED (
    SELECT o.account_id, o.provider_identity_hash, s.message_id, s.fixture_id,
           o.provider_placed_at AS placed_at, o.provider_stake AS stake,
           CASE WHEN receipt.payload->>'net_profit' ~ '^-?[0-9]+(\.[0-9]+)?$'
                     AND pg_input_is_valid(receipt.payload->>'net_profit', 'numeric')
                THEN (receipt.payload->>'net_profit')::numeric END AS net,
           receipt.payload->>'currency' AS currency,
           receipt.payload->>'fees' ~ '^[0-9]+(\.[0-9]+)?$'
               AND pg_input_is_valid(receipt.payload->>'fees', 'numeric') AS has_fee,
           EXISTS (SELECT 1 FROM v2_order_receipt_evidence e
                   WHERE e.order_id=o.id AND e.observed_at BETWEEN o.provider_placed_at AND b.observed_at
                     AND e.payload->>'order_no'=o.provider_order_no
                     AND e.payload->>'state' IN ('accepted', 'unsettled', 'settled')) AS confirmed
    FROM v2_bet_orders o CROSS JOIN bounds b
    LEFT JOIN v2_expert_signals s ON s.id=o.signal_id
    LEFT JOIN LATERAL (
        SELECT e.payload FROM v2_order_receipt_evidence e
        WHERE e.order_id=o.id AND e.payload->>'order_no'=o.provider_order_no AND e.payload->>'state'='settled'
          AND e.observed_at BETWEEN o.provider_placed_at AND b.observed_at
        ORDER BY e.observed_at DESC, e.id DESC LIMIT 1
    ) receipt ON true
    WHERE NOT o.manual AND o.provider_order_no IS NOT NULL
      AND o.provider_placed_at >= b.begin_at AND o.provider_placed_at < b.end_at
      AND b.begin_at < b.end_at AND b.end_at - b.begin_at <= interval '7 days'
      AND b.end_at <= b.observed_at AND b.observed_at <= now()
), coverage AS (
    SELECT count(*) AS fills, count(*) FILTER (WHERE confirmed) AS confirmed_fills,
           count(DISTINCT (account_id,provider_identity_hash)) AS bindings,
           count(*) FILTER (WHERE account_id IS NULL OR provider_identity_hash IS NULL) AS missing_binding,
           count(*) FILTER (WHERE message_id IS NULL OR fixture_id IS NULL) AS missing_context,
           count(*) FILTER (WHERE stake IS NULL OR stake <= 0) AS missing_stake,
           count(DISTINCT currency) AS known_currencies
    FROM fills
), messages AS (
    SELECT date_bin(interval '12 hours', min(placed_at), b.anchor) AS placement_window,
           count(*) AS orders, count(DISTINCT fixture_id) AS fixtures, sum(stake) AS placed_stake,
           count(net) AS settled, sum(net) AS settled_net, sum(stake) FILTER (WHERE net IS NOT NULL) AS settled_stake,
           count(*) FILTER (WHERE net IS NOT NULL AND has_fee AND currency ~ '^[A-Z]{3}$') AS with_cost_fields
    FROM fills CROSS JOIN coverage c CROSS JOIN bounds b
    WHERE c.bindings=1 AND c.missing_binding=0 AND c.missing_context=0 AND c.missing_stake=0 AND c.known_currencies<=1
      AND c.confirmed_fills=c.fills
    GROUP BY message_id, b.anchor
), classified AS (
    SELECT *, CASE WHEN orders=1 THEN 'single'
                   WHEN fixtures=1 THEN 'same_fixture'
                   WHEN orders<=5 THEN 'two_to_five_multifixture'
                   ELSE 'six_plus_multifixture' END AS message_type
    FROM messages
), totals AS (
    SELECT placement_window, message_type, count(*) AS messages, sum(orders) AS orders, sum(placed_stake) AS placed_stake,
           max(placed_stake) AS largest_message_stake, sum(settled) AS settled, sum(settled_net) AS settled_net,
           sum(with_cost_fields) AS with_cost_fields, count(*) FILTER (WHERE orders=settled) AS fully_settled_messages,
           sum(settled_net) FILTER (WHERE orders=settled) AS fully_settled_net,
           sum(settled_stake) FILTER (WHERE orders=settled) AS fully_settled_stake
    FROM classified GROUP BY GROUPING SETS ((message_type), (placement_window,message_type))
)
SELECT jsonb_build_object('bounds',to_jsonb(b),'coverage',to_jsonb(c),
    'results',coalesce((SELECT jsonb_agg(to_jsonb(t) ORDER BY placement_window NULLS FIRST,message_type) FROM totals t),'[]'::jsonb))
FROM bounds b CROSS JOIN coverage c;
ROLLBACK;
