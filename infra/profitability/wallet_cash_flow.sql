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
               count(*) FILTER (WHERE dated_placement) AS placements,
               count(*) FILTER (WHERE dated_settlement) AS settlements,
               count(*) FILTER (WHERE (dated_placement AND provider_stake IS NULL)
                                   OR (dated_settlement AND provider_payout IS NULL)) AS missing_amount,
               count(*) FILTER (WHERE missing_placement OR missing_settlement) AS missing_time,
               count(*) FILTER (WHERE unknown_submission OR possible_late_settlement) AS unresolved,
               count(*) FILTER (WHERE (dated_placement AND provider_placed_at = p.observed_at)
                                   OR (dated_settlement AND settled_at = p.observed_at)) AS at_sample_time
        FROM (
            SELECT provider_stake, provider_payout, provider_placed_at, settled_at,
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
)
SELECT jsonb_build_object(
    'window_start', b.window_start,
    'window_end', b.window_end,
    'samples', (SELECT count(*) FROM samples),
    'first_at', (SELECT min(observed_at) FROM samples),
    'last_at', (SELECT max(observed_at) FROM samples),
    'bindings', c.bindings,
    'missing_identity', c.missing_identity,
    'intervals', coalesce((SELECT jsonb_agg(to_jsonb(r) ORDER BY start_at) FROM result r), '[]'::jsonb)
) FROM coverage c CROSS JOIN bounds b;
ROLLBACK;
