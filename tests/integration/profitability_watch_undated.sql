\ir profitability_watch.sql

UPDATE v2_bet_orders
SET settled_at=NULL,
    receipt_observed_at=CASE WHEN id=3 THEN :'anchor'::timestamptz + interval '48 hours' ELSE receipt_observed_at END,
    provider_placed_at=CASE WHEN id=0 THEN :'anchor'::timestamptz
                           WHEN id IN (2,3) THEN provider_placed_at + interval '1 hour' ELSE provider_placed_at END,
    created_at=CASE WHEN id=0 THEN :'anchor'::timestamptz
                    WHEN id IN (2,3) THEN created_at + interval '1 hour' ELSE created_at END;

UPDATE v2_order_receipt_evidence AS e
SET observed_at=o.receipt_observed_at,
    payload=e.payload || jsonb_build_object('settled_at',NULL,'placed_at',
        CASE WHEN o.id=2 THEN o.provider_placed_at + interval '1 second' ELSE o.provider_placed_at END)
FROM v2_bet_orders o WHERE o.id=e.order_id;

\ir ../../infra/profitability/snapshot.sql
