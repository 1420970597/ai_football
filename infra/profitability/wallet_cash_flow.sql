-- Diagnostic only. The provider's available balance does not enumerate wallet movements.
\if :{?window_start}
\else
\set window_start ''
\endif
\if :{?window_end}
\else
\set window_end ''
\endif
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT nullif(:'window_start', '')::timestamptz AS window_start,
           nullif(:'window_end', '')::timestamptz AS window_end
), samples AS (
    SELECT DISTINCT ON (observed_at) observed_at, balance
    FROM jsonb_array_elements(convert_from(decode(:'wallet_samples', 'base64'), 'UTF8')::jsonb) AS data(sample)
    CROSS JOIN LATERAL (
        SELECT (sample->'wallet'->>'observed_at')::timestamptz AS observed_at,
               (sample->'wallet'->>'available_balance')::numeric AS balance,
               (sample->>'sampled_at')::timestamptz AS sampled_at
    ) parsed
    CROSS JOIN bounds b
    WHERE balance IS NOT NULL AND observed_at BETWEEN sampled_at - interval '10 minutes' AND sampled_at
      AND (b.window_start IS NULL OR observed_at >= b.window_start)
      AND (b.window_end IS NULL OR observed_at <= b.window_end)
    ORDER BY observed_at, sampled_at DESC
), points AS (
    SELECT observed_at, balance,
           lag(observed_at) OVER (ORDER BY observed_at) AS previous_at,
           lag(balance) OVER (ORDER BY observed_at) AS previous_balance
    FROM samples
), sample_span AS (
    SELECT count(*) AS samples, min(observed_at) AS first_at, max(observed_at) AS last_at
    FROM samples
), coverage AS (
    SELECT count(DISTINCT (account_id, provider_identity_hash)) AS bindings,
           count(*) FILTER (WHERE provider_identity_hash IS NULL) AS missing_identity
    FROM v2_bet_orders
    WHERE account_id IS NOT NULL
), intervals AS (
    SELECT p.previous_at AS start_at, p.observed_at AS end_at,
           p.balance - p.previous_balance AS observed_change, details.*
    FROM points p CROSS JOIN coverage c
    CROSS JOIN LATERAL (
        SELECT coalesce(sum(provider_stake) FILTER (WHERE dated_placement), 0) AS placed_stake,
               coalesce(sum(provider_payout) FILTER (WHERE dated_settlement), 0) AS settled_payout,
               CASE WHEN count(*) FILTER (WHERE dated_settlement AND provider_stake IS NULL) = 0
                    THEN coalesce(sum(provider_stake) FILTER (WHERE dated_settlement), 0) END AS settled_principal,
               count(*) FILTER (WHERE dated_settlement AND provider_stake IS NULL) AS missing_settled_principal,
               count(*) FILTER (WHERE dated_placement) AS placements,
               count(*) FILTER (WHERE dated_settlement) AS settlements,
               count(net_difference) AS receipt_net_compared,
               count(*) FILTER (WHERE net_difference <> 0) AS receipt_net_mismatches,
               sum(net_difference) AS receipt_net_difference,
               sum(abs(net_difference)) AS absolute_receipt_net_difference,
               count(*) FILTER (WHERE (dated_placement AND provider_stake IS NULL)
                                   OR (dated_settlement AND provider_payout IS NULL)) AS missing_amount,
               count(*) FILTER (WHERE missing_placement OR missing_settlement) AS missing_time,
               count(*) FILTER (WHERE unknown_submission OR possible_late_settlement) AS unresolved,
               count(*) FILTER (WHERE (dated_placement AND provider_placed_at = p.observed_at)
                                   OR (dated_settlement AND settled_at = p.observed_at)) AS at_sample_time
        FROM (
            SELECT provider_stake, provider_payout, provider_placed_at, settled_at,
                CASE WHEN state = 'settled' AND settled_at > p.previous_at AND settled_at <= p.observed_at
                          AND pnl_basis = 'provider_reported_net'
                     THEN settled_net - (provider_payout - provider_stake) END AS net_difference,
                provider_order_no IS NOT NULL
                    AND provider_placed_at > p.previous_at AND provider_placed_at <= p.observed_at AS dated_placement,
                state = 'settled' AND settled_at > p.previous_at AND settled_at <= p.observed_at AS dated_settlement,
                provider_order_no IS NOT NULL AND provider_placed_at IS NULL
                    AND coalesce(submitted_at, created_at) <= p.observed_at
                    AND (settled_at > p.previous_at OR state <> 'settled'
                         OR (settled_at IS NULL AND receipt_observed_at > p.previous_at)) AS missing_placement,
                state = 'settled' AND settled_at IS NULL
                    AND coalesce(provider_placed_at, submitted_at, created_at) <= p.observed_at
                    AND (receipt_observed_at > p.previous_at OR receipt_observed_at IS NULL) AS missing_settlement,
                state IN ('unknown', 'submitting') AND coalesce(submitted_at, created_at) <= p.observed_at AS unknown_submission,
                state = 'accepted' AND coalesce(provider_placed_at, submitted_at, created_at) <= p.observed_at
                    AS possible_late_settlement
            FROM v2_bet_orders o
            WHERE account_id IS NOT NULL AND state IN ('accepted', 'settled', 'unknown', 'submitting')
        ) events
    ) details
    WHERE p.previous_at IS NOT NULL AND c.bindings = 1 AND c.missing_identity = 0
), result AS (
    SELECT *, observed_change - (settled_payout - placed_stake) AS cash_flow_difference,
           CASE WHEN end_at - start_at > interval '10 minutes' THEN 'sampling_gap'
                WHEN missing_amount + missing_time + unresolved + at_sample_time > 0 THEN 'incomplete'
                WHEN observed_change = settled_payout - placed_stake THEN 'matches_known_orders'
                ELSE 'unexplained_change' END AS evidence
    FROM intervals
), interval_summary AS (
    SELECT count(*) AS compared_intervals,
           count(*) FILTER (WHERE evidence = 'sampling_gap') AS sampling_gaps,
           count(*) FILTER (WHERE evidence = 'incomplete') AS incomplete_intervals,
           count(*) FILTER (WHERE cash_flow_difference <> 0) AS intervals_with_difference,
           sum(placements) AS placements, sum(settlements) AS settlements,
           coalesce(sum(receipt_net_compared), 0) AS receipt_net_compared,
           coalesce(sum(receipt_net_mismatches), 0) AS receipt_net_mismatches,
           sum(receipt_net_difference) AS receipt_net_difference,
           sum(absolute_receipt_net_difference) AS absolute_receipt_net_difference,
           sum(placed_stake) AS placed_stake, sum(settled_payout) AS settled_payout,
           coalesce(sum(missing_settled_principal), 0) AS missing_settled_principal,
           CASE WHEN bool_and(missing_settled_principal = 0) THEN sum(settled_principal) END AS settled_principal,
           CASE WHEN bool_and(missing_settled_principal = 0 AND missing_amount = 0)
                THEN sum(settled_payout - settled_principal) END AS settlement_net_from_payouts,
           CASE WHEN bool_and(missing_settled_principal = 0 AND missing_amount = 0)
                THEN sum(settled_principal - placed_stake) END AS net_principal_released,
           sum(observed_change) AS observed_change,
           sum(cash_flow_difference) AS cash_flow_difference,
           sum(abs(cash_flow_difference)) AS absolute_cash_flow_difference
    FROM result
)
SELECT jsonb_build_object(
    'window_start', b.window_start,
    'window_end', b.window_end,
    'samples', span.samples,
    'first_at', span.first_at,
    'last_at', span.last_at,
    'unobserved_start_seconds', extract(epoch FROM span.first_at - b.window_start),
    'unobserved_end_seconds', extract(epoch FROM b.window_end - span.last_at),
    'bindings', c.bindings,
    'missing_identity', c.missing_identity,
    'comparison', to_jsonb(summary),
    'intervals', coalesce((SELECT jsonb_agg(to_jsonb(r) ORDER BY start_at) FROM result r), '[]'::jsonb)
) FROM coverage c CROSS JOIN bounds b CROSS JOIN sample_span span CROSS JOIN interval_summary summary;
ROLLBACK;
