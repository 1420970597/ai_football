-- Export immutable production preflights for one scheduled research fixture.
-- Run in an explicitly read-only transaction with fixture ID and UTC bounds.
WITH ledger AS (
    SELECT count(DISTINCT (account_id, provider_identity_hash)) AS bindings,
           count(*) FILTER (WHERE account_id IS NULL OR provider_identity_hash IS NULL) AS missing_identity
    FROM v2_bet_orders WHERE provider_order_no IS NOT NULL
), attempts AS (
    SELECT q.fixture_id, f.sport, q.market_id, q.evidence->>'market_kind' AS kind, q.period, q.line::text AS line,
           q.selection, q.captured_at, q.source_updated_at, q.price::text AS price,
           q.evidence->>'provider_quote_checked_at' AS provider_quote_checked_at,
           q.evidence->'native_market_prices' AS native_market_prices,
           q.evidence->'automatic_price_decision' AS automatic_price_decision,
           q.evidence->>'phase' AS phase, q.evidence->>'score_basis' AS score_basis,
           q.evidence->'score_baseline' AS score_baseline,
           o.provider_order_no IS NOT NULL AS native_receipt_present, o.manual,
           CASE
             WHEN o.provider_order_no IS NULL THEN 'no_native_receipt'
             WHEN o.state <> 'settled' THEN 'awaiting_settlement'
             WHEN receipt.observed_at IS NULL OR receipt.observed_at IS DISTINCT FROM o.receipt_observed_at
                  OR receipt.payload IS DISTINCT FROM o.result_payload THEN 'settlement_evidence_mismatch'
             WHEN o.provider_fees IS NULL OR o.provider_currency IS NULL THEN 'settled_missing_cost_or_currency'
             WHEN o.provider_fees <> 0 OR o.pnl_basis IS NULL OR o.pnl_basis = 'unknown'
                  OR o.settled_at IS NULL OR o.provider_stake IS NULL OR o.provider_stake <= 0
                  OR o.provider_payout IS NULL OR o.settled_net IS NULL
                  OR (o.provider_payout - o.provider_stake) IS DISTINCT FROM o.settled_net
                  THEN 'settlement_amounts_unverified'
             ELSE 'locally_matched_receipt'
           END AS settlement_status,
           o.provider_currency AS receipt_currency, o.settled_net::text AS recorded_receipt_net
    FROM v2_quote_evidence q
    JOIN v2_bet_orders o ON o.id=q.reference_id AND q.evidence->>'attempt_id'=o.attempt_id::text
    JOIN v2_expert_signals s ON s.id=o.signal_id AND q.evidence->>'signal_id'=s.id::text
      AND (q.fixture_id,q.market_id,q.period,q.selection)=(s.fixture_id,s.market_id,s.period,s.selection)
      AND q.line IS NOT DISTINCT FROM s.line
    JOIN v2_fixtures f ON f.id=q.fixture_id
    LEFT JOIN LATERAL (
        SELECT e.payload, e.observed_at
        FROM v2_order_receipt_evidence e
        WHERE e.order_id=o.id AND e.payload->>'order_no'=o.provider_order_no
          AND e.payload->>'state'='settled' AND e.observed_at<=transaction_timestamp()
        ORDER BY e.observed_at DESC, e.id DESC LIMIT 1
    ) receipt ON o.state='settled'
    CROSS JOIN ledger l
    WHERE l.bindings=1 AND l.missing_identity=0
      AND EXISTS (SELECT 1 FROM v2_bet_orders filled
                  WHERE filled.provider_order_no IS NOT NULL AND filled.account_id=o.account_id
                    AND filled.provider_identity_hash=o.provider_identity_hash)
      AND q.fixture_id=$1 AND q.captured_at >= $2 AND q.captured_at < $3
      AND $2 < $3 AND $3-$2 <= interval '8 hours' AND $3<=now()
      AND q.evidence_kind='order_attempt' AND q.evidence->>'stage'='preflight_validated'
      AND q.captured_at>=s.created_at
      AND (o.provider_placed_at IS NULL OR q.captured_at<=o.provider_placed_at)
)
SELECT (SELECT coalesce(jsonb_agg(to_jsonb(a) ORDER BY a.captured_at), '[]'::jsonb) FROM attempts a)
FROM ledger l
WHERE l.bindings=1 AND l.missing_identity=0
  AND EXISTS (SELECT 1 FROM v2_fixtures WHERE id=$1)
  AND $2 < $3 AND $3-$2 <= interval '8 hours' AND $3<=now();
