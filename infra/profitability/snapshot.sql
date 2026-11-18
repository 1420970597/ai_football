-- The anchor is fixed at deployment and supplied as a quoted psql variable.
\if :{?archive_from}
\else
\set archive_from -1
\endif
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT :'anchor'::timestamptz AS anchor, now() AS observed_at
), windows AS (
    SELECT tick AS since, tick + interval '12 hours' AS until,
           (EXTRACT(EPOCH FROM tick - anchor) / 43200)::integer AS ordinal
    FROM bounds CROSS JOIN LATERAL (
        SELECT generate_series(anchor, anchor + interval '36 hours', interval '12 hours') AS tick
        UNION
        SELECT generate_series(
            greatest(anchor, date_bin(interval '12 hours', observed_at, anchor) - interval '48 hours'),
            greatest(anchor, date_bin(interval '12 hours', observed_at, anchor)), interval '12 hours'
        )
        UNION
        SELECT generate_series(
            anchor + :archive_from * interval '12 hours',
            least(anchor + (:archive_from + 3) * interval '12 hours',
                  date_bin(interval '12 hours', observed_at, anchor) - interval '12 hours'),
            interval '12 hours'
        ) WHERE :archive_from >= 0
    ) checkpoints
), orders AS MATERIALIZED (
    SELECT o.id, o.account_id, o.provider_identity_hash, o.state, o.manual, o.stake, o.provider_stake, o.reserved_stake,
           o.settled_net, o.provider_payout, o.provider_currency, o.provider_fees, o.pnl_basis,
           o.settled_at, o.receipt_observed_at,
           coalesce(o.provider_placed_at, o.submitted_at, o.created_at) AS placed_at,
           least(o.provider_placed_at, o.submitted_at, o.created_at) AS earliest_placement,
           coalesce(o.settled_at, o.receipt_observed_at) AS booked_at,
           f.sport, s.phase, s.market_kind, s.period, s.selection,
           receipt.observed_at IS NOT NULL AS has_settled_evidence,
           o.provider_order_no IS NOT NULL AND receipt.payload->>'order_no' = o.provider_order_no
               AND receipt.observed_at = o.receipt_observed_at
               AND receipt.payload->>'pnl_basis' = o.pnl_basis
               AND (receipt.payload->>'currency') IS NOT DISTINCT FROM o.provider_currency
               AND (CASE WHEN pg_input_is_valid(receipt.payload->>'stake', 'numeric')
                   THEN (receipt.payload->>'stake')::numeric END) = o.provider_stake
               AND (CASE WHEN pg_input_is_valid(receipt.payload->>'payout', 'numeric')
                   THEN (receipt.payload->>'payout')::numeric END) = o.provider_payout
               AND (CASE WHEN pg_input_is_valid(receipt.payload->>'net_profit', 'numeric')
                   THEN (receipt.payload->>'net_profit')::numeric END) = o.settled_net
               AND (receipt.payload->>'fees' IS NULL AND o.provider_fees IS NULL OR
                   (CASE WHEN pg_input_is_valid(receipt.payload->>'fees', 'numeric')
                       THEN (receipt.payload->>'fees')::numeric END) = o.provider_fees)
               AND (receipt.payload->>'settled_at' IS NULL AND o.settled_at IS NULL OR
                   (CASE WHEN pg_input_is_valid(receipt.payload->>'settled_at', 'timestamptz')
                       THEN (receipt.payload->>'settled_at')::timestamptz END) = o.settled_at) AS receipt_matches
    FROM v2_bet_orders o LEFT JOIN v2_expert_signals s ON s.id = o.signal_id
    LEFT JOIN v2_fixtures f ON f.id = s.fixture_id
    LEFT JOIN LATERAL (
        SELECT e.payload, e.observed_at FROM v2_order_receipt_evidence e
        WHERE e.order_id = o.id AND e.payload->>'state' = 'settled'
        ORDER BY e.observed_at DESC, e.id DESC LIMIT 1
    ) receipt ON o.state = 'settled'
    CROSS JOIN bounds b
    WHERE o.state IN ('submitting', 'unknown', 'accepted')
       OR (o.state = 'settled' AND (coalesce(o.settled_at, o.receipt_observed_at) >= b.anchor - interval '48 hours'
           OR coalesce(o.settled_at, o.receipt_observed_at) IS NULL))
), metrics AS (
    SELECT w.*, observed_at >= until AS closed, nets.*, pending.*, timing.*
    FROM windows w CROSS JOIN bounds b
    CROSS JOIN LATERAL (
        SELECT count(*) AS settled, count(DISTINCT account_id) AS accounts,
               count(DISTINCT provider_identity_hash) AS provider_accounts,
               count(DISTINCT provider_currency) AS currencies,
               min(account_id::text) AS streak_account,
               min(provider_identity_hash) AS streak_provider_identity,
               min(provider_currency) AS streak_currency,
               count(*) FILTER (WHERE provider_currency IS NULL) AS missing_currency,
               count(*) FILTER (WHERE provider_fees IS NULL) AS missing_fee_field,
               count(*) FILTER (WHERE provider_fees <> 0) AS nonzero_fee_basis_unverified,
               count(*) FILTER (WHERE settled_at IS NULL) AS observed_time_only,
               count(*) FILTER (WHERE settled_net IS NULL OR provider_stake IS NULL OR provider_payout IS NULL OR pnl_basis = 'unknown'
                   OR account_id IS NULL OR provider_identity_hash IS NULL OR receipt_observed_at IS NULL) AS missing_receipt_fields,
               count(*) FILTER (WHERE NOT has_settled_evidence) AS missing_settled_evidence,
               count(*) FILTER (WHERE has_settled_evidence AND receipt_matches IS NOT TRUE) AS inconsistent_settled_evidence,
               count(*) FILTER (WHERE provider_payout IS NOT NULL AND settled_net IS NOT NULL
                   AND provider_stake IS NOT NULL AND settled_net <> provider_payout - provider_stake) AS receipt_difference,
               count(*) FILTER (WHERE placed_at < since) AS orders_placed_before_window,
               sum(settled_net) FILTER (WHERE placed_at < since) AS net_from_earlier_orders,
               sum(provider_stake) AS settled_principal, sum(settled_net) AS provider_net
        FROM orders o WHERE state = 'settled' AND booked_at >= w.since AND booked_at < least(w.until, b.observed_at)
    ) nets
    CROSS JOIN LATERAL (
        SELECT count(*) AS pending_from_window,
               count(*) FILTER (WHERE state = 'unknown') AS unknown_from_window,
               sum(reserved_stake) AS reserved_from_window
        FROM orders o WHERE state IN ('accepted', 'submitting', 'unknown') AND placed_at >= w.since AND placed_at < w.until
    ) pending
    CROSS JOIN LATERAL (
        SELECT count(*) AS undated_settlements_overlapping_window
        FROM orders o
        WHERE o.state = 'settled' AND o.settled_at IS NULL
          AND o.earliest_placement < least(w.until, b.observed_at)
          AND (o.receipt_observed_at >= w.since OR o.receipt_observed_at IS NULL)
    ) timing
), classified AS (
    SELECT *, CASE
        WHEN since > (SELECT observed_at FROM bounds) THEN 'upcoming'
        WHEN NOT closed THEN 'open'
        WHEN undated_settlements_overlapping_window > 0 OR observed_time_only > 0 THEN 'unverified'
        WHEN settled = 0 THEN 'no_settlements'
        WHEN accounts <> 1 OR provider_accounts > 1 OR currencies > 1 THEN 'incomparable_accounts_or_currencies'
        WHEN provider_net <= 0 AND missing_receipt_fields = 0 THEN 'nonpositive_receipts'
        WHEN provider_net IS NULL OR missing_receipt_fields > 0 OR receipt_difference > 0
             OR missing_settled_evidence > 0 OR inconsistent_settled_evidence > 0
             OR missing_currency > 0 OR missing_fee_field > 0 OR nonzero_fee_basis_unverified > 0
             OR unknown_from_window > 0 THEN 'unverified'
        ELSE 'positive_receipts'
    END AS status FROM metrics
), streaks AS (
    SELECT name, coalesce(count(*) = 4 AND bool_and(status = 'positive_receipts')
              AND count(DISTINCT streak_account) = 1 AND count(DISTINCT streak_provider_identity) = 1
              AND count(DISTINCT streak_currency) = 1, false) AS verified
    FROM (
        SELECT 'deadline' AS name, c.* FROM classified c WHERE ordinal < 4
        UNION ALL
        SELECT 'rolling', recent.* FROM (
            SELECT * FROM classified WHERE closed ORDER BY ordinal DESC LIMIT 4
        ) recent
    ) streak_windows
    GROUP BY name
), diagnostic AS (
    SELECT sport, phase, market_kind, period, selection, manual, count(*) AS settled, sum(provider_stake) AS principal,
           sum(settled_net) AS provider_net
    FROM orders o CROSS JOIN bounds b
    WHERE state = 'settled' AND booked_at >= b.observed_at - interval '48 hours' AND booked_at < b.observed_at
    GROUP BY 1,2,3,4,5,6
)
SELECT jsonb_build_object(
    'anchor', anchor, 'observed_at', observed_at, 'deadline', anchor + interval '48 hours',
    'deadline_state', CASE WHEN observed_at < anchor + interval '48 hours' THEN 'collecting'
        WHEN (SELECT verified FROM streaks WHERE name = 'deadline')
             AND NOT EXISTS(SELECT 1 FROM orders WHERE state = 'settled' AND booked_at IS NULL)
             AND NOT EXISTS(SELECT 1 FROM orders WHERE state = 'unknown' AND placed_at < anchor + interval '48 hours')
             THEN 'positive_receipts_all_four'
        ELSE 'target_not_verified' END,
    'rolling_four_positive_receipts', coalesce((SELECT verified FROM streaks WHERE name = 'rolling'), false)
        AND NOT EXISTS(SELECT 1 FROM orders WHERE (state = 'settled' AND booked_at IS NULL) OR state = 'unknown'),
    'windows', (SELECT jsonb_agg(to_jsonb(c) - ARRAY['streak_account', 'streak_provider_identity', 'streak_currency']
        ORDER BY ordinal) FROM classified c),
    'settled_without_time', (SELECT count(*) FROM orders WHERE state = 'settled' AND booked_at IS NULL),
    'pending_total', (SELECT count(*) FROM orders WHERE state IN ('submitting','unknown','accepted')),
    'unknown_total', (SELECT count(*) FROM orders WHERE state = 'unknown'),
    'last_48h_by_contract', coalesce((SELECT jsonb_agg(to_jsonb(d) ORDER BY provider_net NULLS LAST) FROM diagnostic d), '[]')
) AS snapshot FROM bounds
\if :{?watch_mode}
\gset
SELECT CASE WHEN :archive_from < 0 THEN coalesce(max((item->>'ordinal')::integer), -1)::text
            ELSE coalesce(string_agg(item->>'ordinal', ' ' ORDER BY (item->>'ordinal')::integer), '') END
FROM jsonb_array_elements(:'snapshot'::jsonb->'windows') AS windows(item)
WHERE (item->>'closed')::boolean;
SELECT :'snapshot'::jsonb;
\else
;
\endif
ROLLBACK;
