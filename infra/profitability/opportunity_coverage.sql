-- Track recommendation coverage before drawing conclusions from filled orders.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT :'begin'::timestamptz AS begin_at, now() AS observed_at
), binding AS (
    SELECT DISTINCT account_id, provider_identity_hash
    FROM v2_bet_orders
    WHERE account_id IS NOT NULL AND provider_order_no IS NOT NULL
), signals AS MATERIALIZED (
    SELECT s.id, s.fixture_id, f.sport, s.phase, s.market_kind, s.period,
           s.withdrawn_at IS NOT NULL AS withdrawn,
           EXISTS (
               SELECT 1 FROM v2_quote_evidence q
               WHERE q.evidence_kind = 'expert_signal' AND q.reference_id = s.id
                 AND q.evidence ? 'market_snapshot'
           ) AS with_snapshot
    FROM v2_expert_signals s LEFT JOIN v2_fixtures f ON f.id = s.fixture_id
    CROSS JOIN bounds b
    WHERE s.created_at >= b.begin_at AND s.created_at < b.observed_at
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
    LEFT JOIN v2_bet_orders o ON o.signal_id = s.id AND NOT o.manual
        AND (o.account_id, o.provider_identity_hash) IN (SELECT account_id, provider_identity_hash FROM binding)
    GROUP BY s.id, s.fixture_id, s.sport, s.phase, s.market_kind, s.period, s.withdrawn, s.with_snapshot
), summary AS (
    SELECT sport, phase, market_kind, period, order_status, withdrawn,
           grouping(sport,phase,market_kind,period) = 15 AS all_markets,
           count(*) AS recommendations, count(DISTINCT fixture_id) AS fixtures,
           count(*) FILTER (WHERE with_snapshot) AS with_snapshot
    FROM classified
    GROUP BY withdrawn, order_status, GROUPING SETS ((), (sport,phase,market_kind,period))
)
SELECT jsonb_build_object('bounds', to_jsonb(b), 'account_bindings', (SELECT count(*) FROM binding),
           'coverage', coalesce((SELECT jsonb_agg(to_jsonb(s) ORDER BY all_markets DESC,
               sport,phase,market_kind,period,order_status,withdrawn) FROM summary s), '[]'::jsonb))
FROM bounds b;
ROLLBACK;
