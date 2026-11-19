-- Export immutable production preflights for one scheduled research fixture.
-- Run in an explicitly read-only transaction with fixture ID and UTC bounds.
WITH ledger AS (
    SELECT count(DISTINCT (account_id, provider_identity_hash)) AS bindings,
           count(*) FILTER (WHERE account_id IS NULL OR provider_identity_hash IS NULL) AS missing_identity
    FROM v2_bet_orders WHERE provider_order_no IS NOT NULL
), attempts AS (
    SELECT q.fixture_id, q.evidence->>'market_kind' AS kind, q.period, q.line::text AS line,
           q.selection, q.captured_at, q.source_updated_at, q.price::text AS price,
           q.evidence->>'phase' AS phase, q.evidence->>'score_basis' AS score_basis,
           q.evidence->'score_baseline' AS score_baseline,
           o.provider_order_no IS NOT NULL AS native_receipt_present
    FROM v2_quote_evidence q
    JOIN v2_bet_orders o ON o.id=q.reference_id AND q.evidence->>'attempt_id'=o.attempt_id::text
    JOIN v2_expert_signals s ON s.id=o.signal_id AND q.evidence->>'signal_id'=s.id::text
      AND (q.fixture_id,q.market_id,q.period,q.selection)=(s.fixture_id,s.market_id,s.period,s.selection)
      AND q.line IS NOT DISTINCT FROM s.line
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
