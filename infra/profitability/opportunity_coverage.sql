-- Track recommendation coverage before drawing conclusions from filled orders.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT :'begin'::timestamptz AS begin_at, now() AS observed_at
), binding AS (
    SELECT DISTINCT account_id, provider_identity_hash
    FROM v2_bet_orders
    WHERE account_id IS NOT NULL AND provider_order_no IS NOT NULL
), signals AS MATERIALIZED (
    SELECT s.id, s.fixture_id, f.sport, nullif(f.competition_id, '') AS competition_id,
           nullif(f.competition_name, '') AS competition_name, s.phase, s.market_kind, s.period, s.score_basis,
           s.withdrawn_at IS NOT NULL AS withdrawn,
           EXISTS (
               SELECT 1 FROM v2_quote_evidence q
               WHERE q.evidence_kind = 'expert_signal' AND q.reference_id = s.id
                 AND q.evidence ? 'market_snapshot'
           ) AS with_snapshot
    FROM v2_expert_signals s LEFT JOIN v2_fixtures f ON f.id = s.fixture_id
    CROSS JOIN bounds b
    WHERE s.created_at >= b.begin_at AND s.created_at < b.observed_at
), automatic_orders AS MATERIALIZED (
    SELECT o.id, o.signal_id, o.attempt_id, o.state, o.provider_order_no,
           coalesce(nullif(o.result_payload->>'reason',''),nullif(o.result_payload->>'category','')) AS reason
    FROM v2_bet_orders o JOIN signals s ON s.id=o.signal_id
    WHERE NOT o.manual
      AND (o.account_id,o.provider_identity_hash) IN (SELECT account_id,provider_identity_hash FROM binding)
), classified AS (
    SELECT s.*,
           CASE WHEN (SELECT count(*) FROM binding) <> 1
                      OR NOT EXISTS (SELECT 1 FROM binding WHERE provider_identity_hash IS NOT NULL)
                THEN 'account_scope_unavailable'
                WHEN count(o.id) = 0 THEN 'no_automatic_order'
                WHEN bool_or(o.provider_order_no IS NOT NULL AND o.state = 'settled') THEN 'settled'
                WHEN bool_or(o.provider_order_no IS NOT NULL AND o.state = 'accepted') THEN 'accepted'
                WHEN bool_or(o.state IN ('submitting','unknown')) THEN 'awaiting_confirmation'
                WHEN bool_or(o.state = 'pending') THEN 'reserved'
                WHEN bool_and(o.state IN ('cancelled','rejected')) THEN 'cancelled_or_rejected'
                ELSE 'unclassified_order_state' END AS order_status
    FROM signals s
    LEFT JOIN automatic_orders o ON o.signal_id=s.id
    GROUP BY s.id, s.fixture_id, s.sport, s.competition_id, s.competition_name,
             s.phase, s.market_kind, s.period, s.score_basis, s.withdrawn, s.with_snapshot
), summary AS (
    SELECT sport, phase, market_kind, period, order_status, withdrawn,
           grouping(sport,phase,market_kind,period) = 15 AS all_markets,
           count(*) AS recommendations, count(DISTINCT fixture_id) AS fixtures,
           count(*) FILTER (WHERE with_snapshot) AS with_snapshot
    FROM classified
    GROUP BY withdrawn, order_status, GROUPING SETS ((), (sport,phase,market_kind,period))
), competition_demand AS (
    SELECT sport, competition_id, min(competition_name) AS competition_name,
           phase, market_kind, period, score_basis,
           count(*) AS recommendations, count(DISTINCT fixture_id) AS fixtures,
           count(*) FILTER (WHERE NOT withdrawn) AS unwithdrawn,
           count(*) FILTER (WHERE with_snapshot) AS with_snapshot,
           count(*) FILTER (WHERE order_status IN ('accepted', 'settled')) AS automatic_fills
    FROM classified
    GROUP BY sport, competition_id, CASE WHEN competition_id IS NULL THEN competition_name END,
             phase, market_kind, period, score_basis
), order_stops AS (
    SELECT s.sport,s.phase,s.market_kind,s.period,o.state,
           CASE WHEN o.reason IN (
                'eligibility_changed','executor_api_rejected','selected_native_market_not_open',
                'selected_native_market_not_open_phase','selected_native_market_not_open_match_over',
                'selected_native_market_not_open_fixture_flag','selected_native_market_not_open_market_flag',
                'selected_native_market_not_unique','selected_native_option_not_open','selected_native_quote_changed',
                'native_limits_not_unique','native_limits_rejected','account_stake_outside_available_limits',
                'account_transport_failure','account_http_rejected','session_expired','executor_live_orders_disabled',
                'native_submission_rejected','native_submission_explicitly_rejected','account_closure_requested',
                'account_rebound','account_unbound','protocol_configuration_reverification','device_authorization',
                'user_disabled','policy_changed','emergency_stop','permission_invalidated',
                'below_minimum_odds','reference_ineligible','reference_unavailable','quote_expired') THEN o.reason
                WHEN o.reason IS NULL THEN 'unrecorded' ELSE 'other_recorded' END AS reason
    FROM automatic_orders o JOIN signals s ON s.id=o.signal_id
    WHERE o.state IN ('cancelled','rejected') AND (SELECT count(*) FROM binding)=1
), stop_summary AS (
    SELECT sport,phase,market_kind,period,state,reason,
           grouping(sport,phase,market_kind,period)=15 AS all_markets, count(*) AS orders
    FROM order_stops GROUP BY state, reason, GROUPING SETS ((), (sport,phase,market_kind,period))
), price_blocker_rows AS (
    SELECT s.sport,s.competition_id,s.competition_name,s.phase,s.fixture_id,o.id AS order_id,o.reason AS status,
           CASE WHEN cause IN (
                'reference_contract_missing','two_complete_sources_missing','settlement_and_cost_approval_missing',
                'two_approved_depth_sources_missing','live_state_unverified','remaining_period_score_mismatch',
                'cross_source_live_state_conflict','live_delay_unresolved','live_event_evidence_conflict',
                'remaining_period_conversion_unverified','contract_terms_unverified','native_offer_check_time_unverified',
                'invalid_approval','ambiguous_approval','reference_validation_failed','offer_cutoff_stale',
                'reference_archive_unavailable','reference_timeout','reference_transport_error','reference_invalid_response'
                ) THEN cause WHEN cause IS NOT NULL THEN 'other_recorded' END AS reason
    FROM automatic_orders o JOIN signals s ON s.id=o.signal_id
    JOIN v2_quote_evidence q ON q.reference_id=o.id AND q.evidence_kind='order_attempt'
         AND q.evidence->>'stage'='preflight_validated'
         AND q.evidence->>'attempt_id'=o.attempt_id::text AND q.evidence->>'signal_id'=s.id::text
    CROSS JOIN LATERAL (SELECT q.evidence->'automatic_price_decision' AS decision) d
    LEFT JOIN LATERAL jsonb_array_elements_text(CASE WHEN jsonb_typeof(d.decision->'reasons')='array'
        THEN d.decision->'reasons' ELSE '[]'::jsonb END) causes(cause) ON true
    WHERE o.state='cancelled' AND o.provider_order_no IS NULL
      AND o.reason IN ('below_minimum_odds','reference_ineligible','reference_unavailable','quote_expired')
      AND d.decision->>'mode'='enforce' AND d.decision->>'status'=o.reason
      AND (SELECT count(*) FROM binding)=1
), price_blockers AS (
    SELECT sport,competition_id,min(competition_name) AS competition_name,phase,status,reason,
           count(DISTINCT order_id) AS orders,count(DISTINCT fixture_id) AS fixtures
    FROM price_blocker_rows
    GROUP BY sport,competition_id,CASE WHEN competition_id IS NULL THEN competition_name END,phase,status,reason
)
SELECT jsonb_build_object('bounds', to_jsonb(b), 'account_bindings', (SELECT count(*) FROM binding),
           'coverage', coalesce((SELECT jsonb_agg(to_jsonb(s) ORDER BY all_markets DESC,
               sport,phase,market_kind,period,order_status,withdrawn) FROM summary s), '[]'::jsonb),
           'competition_demand', coalesce((SELECT jsonb_agg(to_jsonb(d) ORDER BY recommendations DESC,
               sport,competition_id,competition_name,phase,market_kind,period,score_basis)
               FROM competition_demand d), '[]'::jsonb),
           'automatic_order_stops', coalesce((SELECT jsonb_agg(to_jsonb(s) ORDER BY all_markets DESC,
               sport,phase,market_kind,period,state,reason) FROM stop_summary s), '[]'::jsonb),
           'automatic_price_blockers', coalesce((SELECT jsonb_agg(to_jsonb(p) ORDER BY orders DESC,
               sport,competition_id,competition_name,phase,status,reason) FROM price_blockers p), '[]'::jsonb))
FROM bounds b;
ROLLBACK;
