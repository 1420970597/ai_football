-- Isolate price movement from result/contract differences. Emits aggregates only.
\if :{?analysis_end}
\else
\set analysis_end ''
\endif
\if :{?receipt_as_of}
\else
\set receipt_as_of ''
\endif
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT :'anchor'::timestamptz AS anchor, now() AS observed_at,
           least(now(), :'anchor'::timestamptz + interval '48 hours',
                 nullif(:'analysis_end','')::timestamptz) AS analysis_end,
           nullif(:'receipt_as_of','')::timestamptz AS receipt_as_of
), settled AS MATERIALIZED (
    SELECT CASE WHEN coalesce(b.settled_at,b.receipt_observed_at) < a.anchor THEN 'prior_48h'
                ELSE 'initial_48h' END AS cohort,
           date_bin(interval '12 hours',coalesce(b.settled_at,b.receipt_observed_at),a.anchor) AS window_start,
           f.sport, s.phase, s.market_kind, s.period, s.fixture_id, s.id AS signal_id, s.score_basis, b.manual,
           b.account_id, b.provider_identity_hash, b.provider_currency, b.provider_fees,
           b.provider_stake, b.provider_price,
           b.settled_net, b.provider_placed_at, s.created_at AS signal_at,
           s.recommendation_price, s.outcome, s.settlement_reason AS signal_settlement_reason,
           p.price AS preflight_price, p.id AS preflight_id,
           p.fixture_id = s.fixture_id AND p.market_id = s.market_id AND p.period = s.period
               AND p.selection = s.selection AND p.line IS NOT DISTINCT FROM s.line
               AND p.evidence->>'phase' = s.phase AND p.evidence->>'market_kind' = s.market_kind AS native_terms_match,
           p.evidence->>'score_basis' = s.score_basis AS score_basis_matches,
           s.score_baseline->>'home' ~ '^[0-9]+$' AND s.score_baseline->>'away' ~ '^[0-9]+$'
             AND (s.score_baseline->>'home', s.score_baseline->>'away')
             = (p.evidence->'score_baseline'->>'home', p.evidence->'score_baseline'->>'away') AS live_scores_match,
           s.period = 'full_time'
               AND s.score_baseline->>'home' = p.evidence->'score_baseline'->>'home'
               AND s.score_baseline->>'away' = p.evidence->'score_baseline'->>'away'
               AND (s.score_baseline->>'home') || ':' || (s.score_baseline->>'away') = p.evidence->>'score_benchmark'
               AS remaining_baseline_matches,
           rule.basis AS verified_rules_basis, rule.equivalent_period_total, rule.equivalent_remaining_handicap
    FROM v2_bet_orders b LEFT JOIN v2_expert_signals s ON s.id = b.signal_id
    LEFT JOIN v2_fixtures f ON f.id = s.fixture_id CROSS JOIN bounds a
    LEFT JOIN LATERAL (
        SELECT q.id,q.fixture_id,q.market_id,q.period,q.selection,q.line,q.price,q.evidence,q.captured_at
        FROM v2_quote_evidence q
        WHERE q.reference_id = b.id AND q.evidence_kind = 'order_attempt'
            AND q.evidence->>'stage' = 'preflight_validated'
            AND q.evidence->>'attempt_id' = b.attempt_id::text
            AND q.evidence->>'signal_id' = s.id::text
            AND q.captured_at BETWEEN s.created_at AND b.provider_placed_at
        ORDER BY q.captured_at DESC, q.id DESC LIMIT 1
    ) p ON true
    LEFT JOIN LATERAL (
        SELECT coalesce(calc.rule->>'score_basis', calc.equivalent_basis) AS basis,
               calc.rule->>'score_basis' IS NULL AND calc.equivalent_basis='full_period' AS equivalent_period_total,
               calc.rule->>'score_basis' IS NULL AND calc.equivalent_basis='remaining_period' AS equivalent_remaining_handicap
        FROM v2_signal_settlements ss
        CROSS JOIN LATERAL (
            SELECT ss.calculation->'score_rule' AS rule, ss.calculation->'signal' AS inputs,
                   CASE
                       WHEN (s.market_kind,s.market_id,s.period) IN (('total','2','full_time'),('total','18','first_half'))
                           AND ss.reason IN ('asian_line:zero_baseline_score_equivalent', 'asian_split:zero_baseline_score_equivalent',
                                             'asian_line:score_basis_outcome_equivalent', 'asian_split:score_basis_outcome_equivalent')
                           THEN 'full_period'
                       WHEN (s.market_kind,s.market_id,s.period) = ('handicap','4','full_time')
                           AND ss.calculation->'signal'->>'line_basis' = 'provider_home'
                           AND ss.reason IN ('asian_line:zero_baseline_score_equivalent:provider_home_line',
                                             'asian_split:zero_baseline_score_equivalent:provider_home_line',
                                             'asian_line:provider_home_line:score_basis_outcome_equivalent',
                                             'asian_split:provider_home_line:score_basis_outcome_equivalent')
                           THEN 'remaining_period'
                   END AS equivalent_basis
        ) calc
        WHERE ss.signal_id = s.id AND ss.fixture_id = s.fixture_id AND ss.result_version = s.result_version
            AND ss.outcome = s.outcome AND ss.unit_net IS NOT DISTINCT FROM s.unit_net
            AND f.sport = 'football' AND s.fixture_id ~ '^leyu:[0-9]+$'
            AND s.phase = 'live' AND s.score_basis = 'unknown'
            AND ((calc.rule->>'provider' = 'leyu' AND calc.rule->>'market_id' = s.market_id
              AND calc.rule->>'period' = s.period
              AND ss.calculation->'effective_signal'->>'score_basis' = calc.rule->>'score_basis'
              AND (calc.rule->>'reference', calc.rule->>'score_basis', s.market_kind, s.market_id, s.period) IN (
                ('leyu-football-period-total-e330-20261106', 'full_period', 'total', '2', 'full_time'),
                ('leyu-football-period-total-e330-20261106', 'full_period', 'total', '18', 'first_half'),
                ('leyu-football-fulltime-live-handicap-e448-20261106', 'remaining_period', 'handicap', '4', 'full_time')))
              OR ((calc.rule IS NULL OR calc.rule = 'null'::jsonb)
                AND calc.equivalent_basis IS NOT NULL
                AND (calc.equivalent_basis <> 'remaining_period' OR ss.effective_stake = s.effective_stake)
                AND (calc.inputs->>'sport', calc.inputs->>'kind', calc.inputs->>'period', calc.inputs->>'phase',
                     calc.inputs->>'selection', calc.inputs->>'score_basis')
                    = (f.sport, s.market_kind, s.period, s.phase, s.selection, s.score_basis)
                AND calc.inputs->'score_baseline' = s.score_baseline
                AND (CASE WHEN pg_input_is_valid(calc.inputs->>'line','numeric')
                           THEN (calc.inputs->>'line')::numeric END) = s.line
                AND (CASE WHEN pg_input_is_valid(calc.inputs->>'price','numeric')
                           THEN (calc.inputs->>'price')::numeric END) = s.recommendation_price
                AND ss.calculation->'result'->>'status' = 'confirmed'
                AND EXISTS (
                    SELECT 1 FROM v2_quote_evidence permit
                    WHERE permit.reference_id = b.id AND permit.evidence_kind = 'order_attempt'
                      AND permit.evidence->>'stage' = 'submission_authorized'
                      AND permit.evidence->>'attempt_id' = b.attempt_id::text
                      AND permit.evidence->>'signal_id' = s.id::text
                      AND permit.evidence->>'preflight_evidence_id' = p.id::text
                      AND permit.evidence->>'prepared_digest' = p.evidence->>'prepared_digest'
                      AND (permit.fixture_id,permit.market_id,permit.period,permit.selection,permit.line)
                          IS NOT DISTINCT FROM (p.fixture_id,p.market_id,p.period,p.selection,p.line)
                      AND (calc.equivalent_basis <> 'remaining_period' OR (
                           permit.evidence->'score_baseline' = p.evidence->'score_baseline'
                           AND permit.evidence->>'score_benchmark' = p.evidence->>'score_benchmark'))
                      AND permit.captured_at BETWEEN p.captured_at AND b.provider_placed_at
                )
                AND EXISTS (
                    SELECT 1 FROM (
                        SELECT e.payload FROM v2_order_receipt_evidence e
                        WHERE e.order_id = b.id AND e.payload->>'state' = 'settled'
                        ORDER BY e.observed_at DESC,e.id DESC LIMIT 1
                    ) receipt
                    WHERE receipt.payload->>'order_no' = b.provider_order_no
                      AND (CASE WHEN pg_input_is_valid(receipt.payload->>'net_profit','numeric')
                                 THEN (receipt.payload->>'net_profit')::numeric END) = b.settled_net
                )))
        ORDER BY (calc.rule->>'score_basis' IS NULL), ss.created_at DESC, ss.algorithm_version DESC LIMIT 1
    ) rule ON true
    WHERE b.state = 'settled'
        AND coalesce(b.settled_at,b.receipt_observed_at) >= a.anchor - interval '48 hours'
        AND coalesce(b.settled_at,b.receipt_observed_at) < a.analysis_end
        AND (a.receipt_as_of IS NULL OR b.receipt_observed_at <= least(a.receipt_as_of,a.observed_at))
), contract_checked AS (
    SELECT *, CASE
        WHEN signal_id IS NULL THEN 'missing_signal'
        WHEN preflight_id IS NULL THEN 'missing_matching_preflight'
        WHEN native_terms_match IS NOT TRUE THEN 'native_terms_mismatch'
        WHEN score_basis_matches IS NOT TRUE THEN 'score_basis_mismatch'
        WHEN phase <> 'live' OR coalesce(verified_rules_basis, score_basis) = 'full_period' THEN NULL
        WHEN coalesce(verified_rules_basis, score_basis) <> 'remaining_period' THEN 'unverified_live_score_basis'
        WHEN remaining_baseline_matches IS NOT TRUE THEN 'remaining_score_evidence_mismatch'
    END AS contract_exclusion
    FROM settled
), comparable AS (
    SELECT *, contract_exclusion IS NULL AS same_contract,
        CASE
            WHEN contract_exclusion IS NOT NULL THEN contract_exclusion
            WHEN (outcome IN ('win','half_win','loss','half_loss','push','void')) IS NOT TRUE THEN 'unresolved_signal_outcome'
            WHEN (provider_stake > 0 AND settled_net IS NOT NULL) IS NOT TRUE THEN 'missing_receipt_amounts'
            WHEN (provider_price > 1 AND preflight_price > 1 AND recommendation_price > 1) IS NOT TRUE THEN 'missing_valid_prices'
        END AS price_bridge_exclusion,
        CASE outcome WHEN 'win' THEN 1 WHEN 'half_win' THEN 0.5 ELSE 0 END AS win_fraction,
        CASE outcome WHEN 'loss' THEN 1 WHEN 'half_loss' THEN 0.5 ELSE 0 END AS loss_fraction
    FROM contract_checked
), priced AS (
    SELECT *, price_bridge_exclusion IS NULL AS price_bridge_available,
        CASE WHEN price_bridge_exclusion IS NULL THEN
            round(provider_stake * (win_fraction * (recommendation_price - 1) - loss_fraction), 2) END AS at_recommendation,
        CASE WHEN price_bridge_exclusion IS NULL THEN
            round(provider_stake * (win_fraction * (preflight_price - 1) - loss_fraction), 2) END AS at_preflight,
        CASE WHEN price_bridge_exclusion IS NULL THEN
            round(provider_stake * (win_fraction * (provider_price - 1) - loss_fraction), 2) END AS at_fill
    FROM comparable
), summary AS (
    SELECT cohort, window_start, sport, phase, market_kind, manual,
           grouping(sport,phase,market_kind,manual) = 15 AS all_markets,
           count(*) AS settled, count(provider_price) AS fill_prices, count(preflight_price) AS preflight_prices,
           count(*) FILTER (WHERE provider_price < preflight_price) AS worse_than_preflight,
           count(*) FILTER (WHERE provider_price = preflight_price) AS equal_to_preflight,
           count(*) FILTER (WHERE provider_price > preflight_price) AS better_than_preflight,
           count(*) FILTER (WHERE same_contract) AS comparable_recommendations,
           count(*) FILTER (WHERE same_contract AND verified_rules_basis IS NOT NULL) AS comparable_with_verified_live_rule,
           count(*) FILTER (WHERE price_bridge_available AND equivalent_period_total) AS reconstructed_equivalent_period_totals,
           count(*) FILTER (WHERE price_bridge_available AND equivalent_remaining_handicap) AS reconstructed_equivalent_remaining_handicaps,
           count(*) FILTER (WHERE price_bridge_available) AS price_bridge_count,
           count(*) FILTER (WHERE same_contract AND provider_price < recommendation_price) AS worse_than_recommendation,
           count(*) FILTER (WHERE same_contract AND provider_price = recommendation_price) AS equal_to_recommendation,
           count(*) FILTER (WHERE same_contract AND provider_price > recommendation_price) AS better_than_recommendation,
           count(*) FILTER (WHERE provider_placed_at < signal_at) AS negative_latency,
           round((percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM provider_placed_at - signal_at))
               FILTER (WHERE provider_placed_at >= signal_at))::numeric, 2) AS signal_to_fill_p90_seconds,
           sum(provider_stake) AS stake,
           sum(settled_net) AS provider_net,
           sum(at_recommendation) AS diagnostic_net_at_recommendation,
           sum(at_preflight) AS diagnostic_net_at_preflight,
           sum(at_fill) AS diagnostic_net_at_fill,
           sum(at_preflight - at_recommendation) AS recommendation_to_preflight_delta,
           sum(at_fill - at_preflight) AS preflight_to_fill_delta,
           sum(settled_net - at_fill) AS settlement_residual,
           sum(settled_net) FILTER (WHERE price_bridge_available) AS matched_provider_net,
           sum(settled_net) FILTER (WHERE NOT price_bridge_available) AS unmatched_provider_net,
           count(*) FILTER (WHERE settled_net IS NULL) AS missing_receipt_net,
           count(*) FILTER (WHERE abs(settled_net - at_fill) > 0.01) AS residuals_over_one_cent,
           count(*) FILTER (WHERE settled_net = 0 AND abs(at_fill) > 0.01) AS zero_receipts_with_nonzero_reconstruction
    FROM priced
    GROUP BY GROUPING SETS ((cohort), (cohort,sport,phase,market_kind,manual), (cohort,window_start))
), price_directions AS (
    SELECT *, CASE WHEN preflight_price > recommendation_price THEN 'higher'
                   WHEN preflight_price < recommendation_price THEN 'lower' ELSE 'equal' END AS direction
    FROM priced
    WHERE manual IS FALSE AND price_bridge_available AND (phase <> 'live' OR live_scores_match)
), direction_fixtures AS (
    SELECT cohort, window_start, CASE WHEN grouping(phase)=1 THEN 'all' ELSE phase END AS phase, direction,
           count(*) AS settled,
           count(*) FILTER (WHERE provider_currency IS NULL OR provider_fees IS NULL) AS missing_cost_fields,
           sum(provider_stake) AS stake, sum(settled_net) AS receipt_net,
           sum(at_recommendation) AS net_at_recommendation,
           sum(at_preflight - at_recommendation) AS recommendation_to_preflight_delta
    FROM price_directions
    WHERE (SELECT count(DISTINCT (account_id,provider_identity_hash))=1
                  AND bool_and(account_id IS NOT NULL AND provider_identity_hash IS NOT NULL)
                  AND count(DISTINCT provider_currency)<=1 FROM settled WHERE manual IS FALSE)
    GROUP BY cohort, direction, fixture_id, GROUPING SETS ((), (phase), (window_start), (window_start,phase))
), direction_summary AS (
    SELECT cohort, window_start, phase, direction,
           sum(settled) AS settled, count(*) AS fixtures,
           sum(missing_cost_fields) AS missing_cost_fields,
           sum(stake) AS stake, sum(receipt_net) AS receipt_net,
           sum(net_at_recommendation) AS net_at_recommendation,
           sum(recommendation_to_preflight_delta) AS recommendation_to_preflight_delta,
           count(*) FILTER (WHERE receipt_net>0) AS positive_receipt_fixtures,
           max(receipt_net) AS highest_fixture_receipt_net,
           CASE WHEN count(*)>1 THEN sum(receipt_net) - max(receipt_net) END AS receipt_net_without_highest_fixture
    FROM direction_fixtures
    GROUP BY cohort, window_start, phase, direction
), exclusions AS (
    SELECT cohort, window_start, sport, phase, market_kind, period, price_bridge_exclusion AS reason, signal_settlement_reason,
           count(*) AS orders, count(settled_net) AS with_receipt_net, sum(settled_net) AS provider_net
    FROM priced
    WHERE price_bridge_exclusion IS NOT NULL
    GROUP BY cohort, sport, phase, market_kind, period, price_bridge_exclusion, signal_settlement_reason,
             GROUPING SETS ((), (window_start))
)
SELECT jsonb_build_object('anchor',anchor,'observed_at',observed_at,'analysis_end',analysis_end,
    'receipt_as_of',receipt_as_of,'cohorts',coalesce((
    SELECT jsonb_agg(to_jsonb(s) - 'window_start' ORDER BY cohort, all_markets DESC, sport, phase, market_kind, manual)
    FROM summary s WHERE s.window_start IS NULL
), '[]'::jsonb), 'price_bridge_exclusions', coalesce((
    SELECT jsonb_agg(to_jsonb(e) - 'window_start' ORDER BY cohort, reason, sport, phase, market_kind, period, signal_settlement_reason)
    FROM exclusions e WHERE e.window_start IS NULL
), '[]'::jsonb), 'windows', coalesce((
    SELECT jsonb_agg(to_jsonb(s) - ARRAY['sport','phase','market_kind','manual','all_markets'] ||
        jsonb_build_object('window_end',s.window_start + interval '12 hours',
                           'closed',s.window_start + interval '12 hours' <= analysis_end)
        ORDER BY s.window_start)
    FROM summary s WHERE s.window_start IS NOT NULL
), '[]'::jsonb), 'window_price_bridge_exclusions', coalesce((
    SELECT jsonb_agg(to_jsonb(e) ORDER BY window_start, reason, sport, phase, market_kind, period, signal_settlement_reason)
    FROM exclusions e WHERE e.window_start IS NOT NULL
), '[]'::jsonb), 'preflight_direction_outcomes', coalesce((
    SELECT jsonb_agg(to_jsonb(d) ORDER BY cohort, window_start NULLS FIRST, phase, direction)
    FROM direction_summary d
), '[]'::jsonb)) FROM bounds;
ROLLBACK;
