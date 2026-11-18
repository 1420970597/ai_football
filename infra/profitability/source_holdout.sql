-- Freeze loss-based rules before the holdout. Replay existing automatic fills only.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT :'begin'::timestamptz AS begin_at, :'split'::timestamptz AS split_at,
           :'end'::timestamptz AS end_at, :'observed'::timestamptz AS observed_at
), candidates AS MATERIALIZED (
    SELECT o.account_id, o.provider_identity_hash, o.provider_placed_at AS placed_at,
           o.provider_stake AS stake, m.source_id, s.expert_id, s.fixture_id, s.phase,
           outcome.observed_at, outcome.payload->>'currency' AS currency,
           outcome.payload->>'fees' IS NOT NULL AS has_fee,
           CASE WHEN pg_input_is_valid(outcome.payload->>'net_profit', 'numeric')
                THEN (outcome.payload->>'net_profit')::numeric END AS net,
           o.provider_placed_at < b.split_at AS training
    FROM v2_bet_orders o CROSS JOIN bounds b
    LEFT JOIN v2_expert_signals s ON s.id = o.signal_id
    LEFT JOIN v2_expert_messages m ON m.id = s.message_id
    LEFT JOIN LATERAL (
        SELECT e.observed_at, e.payload FROM v2_order_receipt_evidence e
        WHERE e.order_id = o.id AND e.payload->>'order_no' = o.provider_order_no
            AND e.payload->>'state' = 'settled'
            AND e.observed_at < CASE WHEN o.provider_placed_at < b.split_at THEN b.split_at ELSE b.observed_at END
        ORDER BY e.observed_at DESC, e.id DESC LIMIT 1
    ) outcome ON true
    WHERE NOT o.manual AND o.provider_order_no IS NOT NULL
      AND coalesce(o.provider_placed_at, o.created_at) >= b.begin_at
      AND coalesce(o.provider_placed_at, o.created_at) < b.end_at
      AND b.begin_at < b.split_at AND b.split_at < b.end_at AND b.end_at <= b.observed_at AND b.observed_at <= now()
), coverage AS (
    SELECT count(*) FILTER (WHERE training) AS training_fills,
           count(net) FILTER (WHERE training) AS training_with_net,
           count(*) FILTER (WHERE NOT training) AS holdout_fills,
           count(net) FILTER (WHERE NOT training) AS holdout_with_net,
           count(*) FILTER (WHERE placed_at IS NULL OR source_id IS NULL OR expert_id IS NULL OR fixture_id IS NULL
                                 OR account_id IS NULL OR provider_identity_hash IS NULL OR stake IS NULL
                                 OR stake <= 0 OR observed_at < placed_at) AS invalid_fills,
           count(DISTINCT (account_id, provider_identity_hash)) AS account_bindings,
           count(DISTINCT currency) AS known_currencies
    FROM candidates
), training AS (
    SELECT source_id, expert_id, count(DISTINCT fixture_id) AS fixtures,
           count(DISTINCT (placed_at AT TIME ZONE 'UTC')::date) AS days,
           sum(net / nullif(stake, 0)) AS flat_units
    FROM candidates
    WHERE training AND net IS NOT NULL
    GROUP BY GROUPING SETS ((source_id), (source_id, expert_id))
    HAVING count(DISTINCT fixture_id) >= 20
        AND count(DISTINCT (placed_at AT TIME ZONE 'UTC')::date) >= 3
        AND sum(net / nullif(stake, 0)) < 0
), holdout AS (
    SELECT c.*, source_loss.source_id IS NOT NULL AS exclude_source,
           author_loss.source_id IS NOT NULL AS exclude_source_author
    FROM candidates c CROSS JOIN coverage e
    LEFT JOIN training source_loss ON source_loss.source_id = c.source_id AND source_loss.expert_id IS NULL
    LEFT JOIN training author_loss ON (author_loss.source_id, author_loss.expert_id) = (c.source_id, c.expert_id)
    WHERE NOT c.training AND e.invalid_fills = 0 AND e.account_bindings = 1 AND e.known_currencies <= 1
), results AS (
    SELECT rule, CASE WHEN grouping(phase) = 1 THEN 'all' ELSE phase END AS phase,
           count(*) AS opportunities, count(*) FILTER (WHERE removed) AS removed,
           count(net) FILTER (WHERE NOT removed) AS retained_with_net,
           count(*) FILTER (WHERE currency IS NULL OR NOT has_fee) AS without_cost_evidence,
           sum(net) FILTER (WHERE NOT removed) AS receipt_net,
           sum(stake) FILTER (WHERE NOT removed AND net IS NOT NULL) AS settled_stake,
           round(avg(net / nullif(stake, 0)) FILTER (WHERE NOT removed) * 100, 2) AS equal_stake_receipt_pct
    FROM holdout
    CROSS JOIN LATERAL (VALUES ('baseline', false), ('exclude_source', exclude_source),
                              ('exclude_source_author', exclude_source_author)) AS rules(rule, removed)
    GROUP BY rule, GROUPING SETS ((), (phase))
)
SELECT jsonb_build_object('bounds', to_jsonb(b), 'coverage', to_jsonb(c),
    'training_exclusions', (SELECT jsonb_build_object(
        'sources', count(*) FILTER (WHERE expert_id IS NULL),
        'source_authors', count(*) FILTER (WHERE expert_id IS NOT NULL)) FROM training),
    'results', coalesce((SELECT jsonb_agg(to_jsonb(r) ORDER BY rule, phase) FROM results r), '[]'::jsonb))
FROM bounds b CROSS JOIN coverage c;
ROLLBACK;
