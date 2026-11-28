-- Compare natural fills to the retained recommendation-time price set.
\if :{?end}
\else
\set end ''
\endif
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT :'begin'::timestamptz AS begin_at, now() AS observed_at,
           least(now(), nullif(:'end','')::timestamptz) AS end_at
), fills AS (
    SELECT f.sport,s.phase,s.market_kind,s.period,s.fixture_id,o.id AS order_id,o.provider_order_no,
           o.manual,o.state,o.provider_price AS fill_price,
           p.price AS preflight_price,s.recommendation_price,
           e.id IS NOT NULL AS has_snapshot, p.id IS NOT NULL AS has_preflight,
           p.fixture_id = s.fixture_id AND p.market_id = s.market_id AND p.period = s.period
             AND p.line IS NOT DISTINCT FROM s.line AND p.selection = s.selection
             AND p.evidence->>'phase' = s.phase AND p.evidence->>'market_kind' = s.market_kind
             AND p.evidence->>'score_basis' = s.score_basis
             AND p.evidence->>'market_instance_id' = snapshot.data->>'market_instance_id'
             AND p.evidence->>'play_id' = snapshot.data->>'play_id'
             AND p.evidence->>'selection_id' = options.selected_id AS same_snapshot_contract,
           s.phase <> 'live' OR (
               s.score_baseline->>'home' IS NOT NULL AND s.score_baseline->>'away' IS NOT NULL
               AND p.evidence->'score_baseline'->>'home' IS NOT NULL
               AND p.evidence->'score_baseline'->>'away' IS NOT NULL
               AND p.evidence->>'score_benchmark' IS NOT NULL
           ) AS has_live_score,
           s.phase <> 'live' OR (
               s.score_baseline->>'home' = p.evidence->'score_baseline'->>'home'
               AND s.score_baseline->>'away' = p.evidence->'score_baseline'->>'away'
               AND concat(s.score_baseline->>'home', ':', s.score_baseline->>'away') = p.evidence->>'score_benchmark'
           ) AS same_live_score,
           s.phase <> 'live' OR s.period = 'full_time' OR (
               -- Native market 18 settles on the entire first half.
               s.period = 'first_half' AND f.sport = 'football' AND s.fixture_id ~ '^leyu:[0-9]+$'
               AND s.market_id = '18' AND s.market_kind = 'total'
               AND s.score_basis IN ('unknown', 'full_period')
           ) AS supported_live_period,
           options.selected_price = s.recommendation_price
             AND options.sides = CASE
                 WHEN s.market_kind = 'winner' AND f.sport = 'football' THEN ARRAY['away','draw','home']
                 WHEN s.market_kind IN ('winner','handicap') THEN ARRAY['away','home']
                 WHEN s.market_kind IN ('total','team_total') THEN ARRAY['over','under'] END
             AND options.valid_prices = cardinality(options.sides)
             AND options.unique_ids = cardinality(options.sides) AS valid_options,
           options.inverse_sum,
           e.captured_at = s.created_at AND e.evidence->>'market_kind' = s.market_kind
             AND e.evidence->>'score_basis' = s.score_basis AS same_signal_snapshot,
           s.outcome,
           CASE s.outcome WHEN 'win' THEN 1 WHEN 'half_win' THEN 0.5 ELSE 0 END AS win_fraction,
           CASE s.outcome WHEN 'loss' THEN 1 WHEN 'half_loss' THEN 0.5 ELSE 0 END AS loss_fraction,
           s.settled_at IS NOT NULL AND EXISTS (
               SELECT 1 FROM v2_signal_settlements settled
               JOIN v2_fixture_results result ON result.fixture_id=settled.fixture_id AND result.version=settled.result_version
               WHERE settled.signal_id=s.id AND settled.fixture_id=s.fixture_id AND settled.result_version=s.result_version
                 AND settled.outcome=s.outcome AND settled.unit_net IS NOT DISTINCT FROM s.unit_net
                 AND settled.effective_stake=s.effective_stake AND settled.reason=s.settlement_reason
                 AND result.status IN ('confirmed','void')
                 AND result.version=(SELECT max(version) FROM v2_fixture_results WHERE fixture_id=s.fixture_id)
           ) AS settlement_valid,
           o.settled_net, o.provider_stake, o.provider_payout, o.settled_at,
           o.provider_fees, o.provider_currency, o.account_id, o.provider_identity_hash
    FROM v2_bet_orders o JOIN v2_expert_signals s ON s.id = o.signal_id
    LEFT JOIN v2_fixtures f ON f.id = s.fixture_id CROSS JOIN bounds b
    LEFT JOIN LATERAL (
        SELECT q.id, q.evidence, q.captured_at
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
    WHERE s.created_at >= b.begin_at AND s.created_at < b.end_at
      AND o.provider_order_no IS NOT NULL AND o.state IN ('accepted','settled')
), classified AS (
    SELECT *, CASE
        WHEN NOT has_snapshot THEN 'missing_snapshot'
        WHEN NOT has_preflight THEN 'missing_matching_preflight'
        WHEN valid_options IS NOT TRUE THEN 'invalid_snapshot_options'
        WHEN same_snapshot_contract IS NOT TRUE THEN 'snapshot_contract_mismatch'
        WHEN has_live_score IS NOT TRUE THEN 'missing_live_score'
        WHEN same_live_score IS NOT TRUE THEN 'live_score_mismatch'
        WHEN supported_live_period IS NOT TRUE THEN 'unsupported_live_period_or_basis'
        WHEN (fill_price > 1 AND preflight_price > 1) IS NOT TRUE THEN 'missing_valid_prices'
    END AS exclusion
    FROM fills
), compared AS (
    SELECT *, exclusion IS NULL AS comparable FROM classified
), margin_payouts AS (
    SELECT *, provider_stake * win_fraction AS winning_stake,
           provider_stake * (win_fraction * (fill_price - 1) - loss_fraction) AS gross_net_at_fill
    FROM compared WHERE state='settled' AND comparable
), margin_evidence AS (
    SELECT *, CASE
        WHEN same_signal_snapshot IS NOT TRUE THEN 'snapshot_identity_unverified'
        WHEN settlement_valid IS NOT TRUE
          OR (outcome IN ('win','half_win','loss','half_loss','push','void')) IS NOT TRUE THEN 'settlement_unverified'
        WHEN (provider_stake > 0 AND provider_payout >= 0) IS NOT TRUE THEN 'payout_unavailable'
        WHEN abs(provider_payout - round(provider_stake + gross_net_at_fill,2)) > 0.01
          THEN 'payout_conflicts_with_reconstruction'
    END AS margin_exclusion
    FROM margin_payouts
), margin_receipts AS (
    SELECT m.*, (m.margin_exclusion='payout_conflicts_with_reconstruction'
             AND m.provider_payout=m.provider_stake AND m.settled_net=0
             AND receipt.payload->'provider_settlement_codes' @> '{"betResult":0,"betStatus":5,"orderStatus":4}'
             AND receipt.payload->'provider_settlement_codes'->>'cancelType' ~ '^[1-9][0-9]{0,8}$'
             AND (CASE WHEN pg_input_is_valid(receipt.payload->>'stake','numeric')
                       THEN (receipt.payload->>'stake')::numeric END)=m.provider_stake
             AND (CASE WHEN pg_input_is_valid(receipt.payload->>'payout','numeric')
                       THEN (receipt.payload->>'payout')::numeric END)=m.provider_payout
             AND (CASE WHEN pg_input_is_valid(receipt.payload->>'net_profit','numeric')
                       THEN (receipt.payload->>'net_profit')::numeric END)=m.settled_net
             ) IS TRUE AS native_cancellation_return
    FROM margin_evidence m CROSS JOIN bounds b
    LEFT JOIN LATERAL (
        SELECT e.payload FROM v2_order_receipt_evidence e
        WHERE m.margin_exclusion='payout_conflicts_with_reconstruction'
          AND m.provider_payout=m.provider_stake AND m.settled_net=0
          AND e.order_id=m.order_id AND e.payload->>'order_no'=m.provider_order_no
          AND e.payload->>'state'='settled' AND e.observed_at<=b.observed_at
        ORDER BY e.observed_at DESC,e.id DESC LIMIT 1
    ) receipt ON true
), margin_summary AS (
    SELECT CASE WHEN grouping(phase)=1 THEN 'all' ELSE phase END AS phase,
           count(*) AS price_comparable_settled,
           count(*) FILTER (WHERE margin_exclusion IS NULL) AS with_verified_payout,
           count(DISTINCT fixture_id) FILTER (WHERE margin_exclusion IS NULL) AS verified_fixtures,
           count(*) FILTER (WHERE margin_exclusion='snapshot_identity_unverified') AS snapshot_identity_unverified,
           count(*) FILTER (WHERE margin_exclusion='settlement_unverified') AS settlement_unverified,
           count(*) FILTER (WHERE margin_exclusion='payout_unavailable') AS payout_unavailable,
           count(*) FILTER (WHERE margin_exclusion='payout_conflicts_with_reconstruction')
               AS payout_conflicts_with_reconstruction,
           count(*) FILTER (WHERE native_cancellation_return) AS payout_conflicts_with_native_cancellation_return,
           count(*) FILTER (WHERE margin_exclusion IS NULL AND (provider_currency IS NULL OR provider_fees IS NULL))
               AS verified_payout_missing_cost_fields,
           CASE WHEN count(DISTINCT (account_id,provider_identity_hash))=1
                     AND bool_and(account_id IS NOT NULL AND provider_identity_hash IS NOT NULL)
                     AND count(DISTINCT provider_currency)<=1 THEN
               jsonb_build_object(
                   'stake', sum(provider_stake) FILTER (WHERE margin_exclusion IS NULL),
                   'observed_payout_minus_stake', sum(provider_payout-provider_stake) FILTER (WHERE margin_exclusion IS NULL),
                   'quoted_margin_drag', round(sum(winning_stake * recommendation_price * (inverse_sum-1))
                       FILTER (WHERE margin_exclusion IS NULL),2),
                   'recommendation_to_preflight_effect', round(sum(winning_stake * (preflight_price-recommendation_price))
                       FILTER (WHERE margin_exclusion IS NULL),2),
                   'preflight_to_fill_effect', round(sum(winning_stake * (fill_price-preflight_price))
                       FILTER (WHERE margin_exclusion IS NULL),2)
               )
           END AS amounts
    FROM margin_receipts GROUP BY GROUPING SETS ((), (phase))
), exclusions AS (
    SELECT sport, phase, market_kind, period, exclusion, count(*) AS fills,
           count(*) FILTER (WHERE state = 'settled') AS settled
    FROM classified WHERE exclusion IS NOT NULL
    GROUP BY sport, phase, market_kind, period, exclusion
), price_direction AS (
    SELECT phase, CASE WHEN preflight_price > recommendation_price THEN 'higher'
                       WHEN preflight_price < recommendation_price THEN 'lower' ELSE 'equal' END AS direction,
           fixture_id, manual, settled_net, provider_stake, provider_payout, settled_at, provider_fees,
           provider_currency, account_id, provider_identity_hash, state,
           state='settled' AND settled_net IS NOT NULL AND provider_stake > 0 AS usable_receipt
    FROM compared WHERE comparable
), preflight_outcomes AS (
    SELECT CASE WHEN grouping(phase) = 1 THEN 'all' ELSE phase END AS phase, direction, automatic_only,
           count(*) AS fills, count(*) FILTER (WHERE state='settled') AS settled,
           count(*) FILTER (WHERE state='accepted') AS pending,
           count(*) FILTER (WHERE usable_receipt) AS with_amounts,
           count(DISTINCT fixture_id) FILTER (WHERE usable_receipt) AS settled_fixtures_with_amounts,
           count(*) FILTER (WHERE usable_receipt AND settled_net>0) AS positive_receipts,
           count(*) FILTER (WHERE usable_receipt AND settled_net<0) AS negative_receipts,
           count(*) FILTER (WHERE usable_receipt AND settled_net=0) AS zero_receipts,
           count(*) FILTER (WHERE usable_receipt AND settled_net=0 AND provider_payout=provider_stake)
               AS zero_with_principal_return,
           count(*) FILTER (WHERE usable_receipt AND settled_net=0 AND settled_at IS NULL)
               AS zero_without_native_settlement_time,
           count(*) FILTER (WHERE state='settled' AND (provider_fees IS NULL OR provider_currency IS NULL)) AS missing_cost_fields,
           CASE WHEN count(DISTINCT (account_id, provider_identity_hash))=1
                     AND bool_and(account_id IS NOT NULL AND provider_identity_hash IS NOT NULL)
                     AND count(DISTINCT provider_currency)<=1
                THEN sum(provider_stake) FILTER (WHERE usable_receipt)
           END AS settled_stake,
           CASE WHEN count(DISTINCT (account_id, provider_identity_hash))=1
                     AND bool_and(account_id IS NOT NULL AND provider_identity_hash IS NOT NULL)
                     AND count(DISTINCT provider_currency)<=1
                THEN sum(settled_net) FILTER (WHERE usable_receipt)
           END AS receipt_net
    FROM price_direction
    CROSS JOIN (VALUES (false), (true)) AS scope(automatic_only)
    WHERE NOT automatic_only OR manual IS FALSE
    GROUP BY direction, automatic_only, GROUPING SETS ((), (phase))
), preflight_results AS (
    SELECT automatic_only, phase, direction, to_jsonb(p) - 'automatic_only' ||
           jsonb_build_object('receipt_roi_percent',round(100 * receipt_net / nullif(settled_stake,0),2)) AS data
    FROM preflight_outcomes p
), automatic_coverage AS (
    SELECT CASE WHEN grouping(phase)=1 THEN 'all' ELSE phase END AS phase,
           count(*) AS fills,
           count(*) FILTER (WHERE comparable) AS comparable_fills,
           count(*) FILTER (WHERE NOT comparable) AS excluded_fills,
           count(*) FILTER (WHERE state='accepted') AS pending,
           count(*) FILTER (WHERE state='settled' AND comparable) AS comparable_settled,
           count(*) FILTER (WHERE state='settled' AND NOT comparable) AS excluded_settled,
           count(*) FILTER (WHERE state='settled' AND NOT comparable AND settled_net IS NULL)
               AS excluded_missing_receipt_net,
           count(*) FILTER (WHERE state='settled' AND NOT comparable AND (provider_fees IS NULL OR provider_currency IS NULL))
               AS excluded_missing_cost_fields,
           CASE WHEN count(DISTINCT (account_id, provider_identity_hash))=1
                     AND bool_and(account_id IS NOT NULL AND provider_identity_hash IS NOT NULL)
                     AND count(DISTINCT provider_currency)<=1
                THEN sum(settled_net) FILTER (WHERE state='settled' AND NOT comparable)
           END AS excluded_receipt_net
    FROM compared WHERE manual IS FALSE
    GROUP BY GROUPING SETS ((), (phase))
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
SELECT jsonb_build_object('bounds',to_jsonb(b),
    'comparison',(SELECT jsonb_agg(to_jsonb(s) ORDER BY all_markets DESC,sport,phase,market_kind,period)
        FROM summary s),
    'price_exclusions',coalesce((SELECT jsonb_agg(to_jsonb(e) ORDER BY sport,phase,market_kind,period,exclusion)
        FROM exclusions e),'[]'::jsonb),
    'preflight_outcomes',coalesce((SELECT jsonb_agg(data ORDER BY phase,direction)
        FROM preflight_results WHERE NOT automatic_only),'[]'::jsonb),
    'automatic_preflight_outcomes',coalesce((SELECT jsonb_agg(data ORDER BY phase,direction)
        FROM preflight_results WHERE automatic_only),'[]'::jsonb),
    'realized_quote_margin',coalesce((SELECT jsonb_agg(to_jsonb(m) ORDER BY phase) FROM margin_summary m),'[]'::jsonb),
    'automatic_coverage',(SELECT jsonb_agg(to_jsonb(a) ORDER BY phase) FROM automatic_coverage a)
) FROM bounds b;
ROLLBACK;
