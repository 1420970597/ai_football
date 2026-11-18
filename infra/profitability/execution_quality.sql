-- Isolate price movement from result/contract differences. Emits aggregates only.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT :'anchor'::timestamptz AS anchor, now() AS observed_at
), settled AS MATERIALIZED (
    SELECT CASE WHEN coalesce(b.settled_at,b.receipt_observed_at) < a.anchor THEN 'prior_48h'
                ELSE 'initial_48h' END AS cohort,
           f.sport, s.phase, s.market_kind, b.manual, b.provider_stake, b.provider_price,
           b.settled_net, b.provider_placed_at, s.created_at AS signal_at,
           s.recommendation_price, s.outcome, p.price AS preflight_price,
           p.fixture_id = s.fixture_id AND p.market_id = s.market_id AND p.period = s.period
               AND p.selection = s.selection AND p.line IS NOT DISTINCT FROM s.line
               AND p.evidence->>'phase' = s.phase AND p.evidence->>'market_kind' = s.market_kind
               AND p.evidence->>'score_basis' = s.score_basis
               AND (s.phase <> 'live' OR s.score_basis = 'full_period') AS same_contract
    FROM v2_bet_orders b LEFT JOIN v2_expert_signals s ON s.id = b.signal_id
    LEFT JOIN v2_fixtures f ON f.id = s.fixture_id CROSS JOIN bounds a
    LEFT JOIN LATERAL (
        SELECT q.fixture_id,q.market_id,q.period,q.selection,q.line,q.price,q.evidence
        FROM v2_quote_evidence q
        WHERE q.reference_id = b.id AND q.evidence_kind = 'order_attempt'
            AND q.evidence->>'stage' = 'preflight_validated'
            AND q.evidence->>'attempt_id' = b.attempt_id::text
        ORDER BY q.captured_at DESC, q.id DESC LIMIT 1
    ) p ON true
    WHERE b.state = 'settled'
        AND coalesce(b.settled_at,b.receipt_observed_at) >= a.anchor - interval '48 hours'
        AND coalesce(b.settled_at,b.receipt_observed_at) < least(a.observed_at,a.anchor + interval '48 hours')
), comparable AS (
    SELECT *, same_contract AND provider_stake IS NOT NULL AND provider_price > 1 AND recommendation_price > 1
                  AND outcome IN ('win','half_win','loss','half_loss','push','void') AS price_bridge_available,
        CASE outcome WHEN 'win' THEN 1 WHEN 'half_win' THEN 0.5 ELSE 0 END AS win_fraction,
        CASE outcome WHEN 'loss' THEN 1 WHEN 'half_loss' THEN 0.5 ELSE 0 END AS loss_fraction
    FROM settled
), summary AS (
    SELECT cohort, sport, phase, market_kind, manual, grouping(sport,phase,market_kind,manual) = 15 AS all_markets,
           count(*) AS settled, count(provider_price) AS fill_prices, count(preflight_price) AS preflight_prices,
           count(*) FILTER (WHERE provider_price < preflight_price) AS worse_than_preflight,
           count(*) FILTER (WHERE provider_price = preflight_price) AS equal_to_preflight,
           count(*) FILTER (WHERE provider_price > preflight_price) AS better_than_preflight,
           count(*) FILTER (WHERE same_contract) AS comparable_recommendations,
           count(*) FILTER (WHERE price_bridge_available) AS price_bridge_count,
           count(*) FILTER (WHERE same_contract AND provider_price < recommendation_price) AS worse_than_recommendation,
           count(*) FILTER (WHERE same_contract AND provider_price = recommendation_price) AS equal_to_recommendation,
           count(*) FILTER (WHERE same_contract AND provider_price > recommendation_price) AS better_than_recommendation,
           count(*) FILTER (WHERE provider_placed_at < signal_at) AS negative_latency,
           round((percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM provider_placed_at - signal_at))
               FILTER (WHERE provider_placed_at >= signal_at))::numeric, 2) AS signal_to_fill_p90_seconds,
           sum(provider_stake) AS stake,
           sum(settled_net) AS provider_net,
           sum(round(provider_stake * (win_fraction * (recommendation_price - 1) - loss_fraction),2))
               FILTER (WHERE price_bridge_available) AS diagnostic_net_at_recommendation,
           sum(round(provider_stake * (win_fraction * (provider_price - 1) - loss_fraction),2))
               FILTER (WHERE price_bridge_available) AS diagnostic_net_at_fill,
           sum(settled_net) FILTER (WHERE price_bridge_available) AS matched_provider_net
    FROM comparable
    GROUP BY GROUPING SETS ((cohort), (cohort,sport,phase,market_kind,manual))
)
SELECT jsonb_build_object('anchor',anchor,'observed_at',observed_at,'cohorts',coalesce((
    SELECT jsonb_agg(to_jsonb(s) ORDER BY cohort, all_markets DESC, sport, phase, market_kind, manual) FROM summary s
), '[]'::jsonb)) FROM bounds;
ROLLBACK;
