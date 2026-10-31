-- Official account receipts remain distinct from the platform reservation.
ALTER TABLE v2_bet_orders ADD COLUMN provider_price NUMERIC(12,5) CHECK(provider_price > 1);
ALTER TABLE v2_bet_orders ADD COLUMN provider_stake NUMERIC(16,5) CHECK(provider_stake >= 0);
ALTER TABLE v2_bet_orders ADD COLUMN provider_payout NUMERIC(16,5) CHECK(provider_payout >= 0);
ALTER TABLE v2_bet_orders ADD COLUMN provider_currency TEXT CHECK(provider_currency ~ '^[A-Z]{3}$');
ALTER TABLE v2_bet_orders ADD COLUMN provider_fees NUMERIC(16,5);
ALTER TABLE v2_bet_orders ADD COLUMN provider_placed_at TIMESTAMPTZ;
ALTER TABLE v2_bet_orders ADD COLUMN receipt_observed_at TIMESTAMPTZ;
ALTER TABLE v2_bet_orders ADD COLUMN pnl_basis TEXT NOT NULL DEFAULT 'unknown'
  CHECK(pnl_basis IN('unknown','provider_reported_net','gross_return_minus_stake'));
CREATE TABLE v2_order_receipt_evidence(
 id UUID PRIMARY KEY,
 order_id UUID NOT NULL REFERENCES v2_bet_orders(id),
 receipt_sha256 TEXT NOT NULL CHECK(receipt_sha256 ~ '^[a-f0-9]{64}$'),
 payload JSONB NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(order_id,receipt_sha256)
);
CREATE INDEX v2_order_receipt_order_time_idx ON v2_order_receipt_evidence(order_id,observed_at,id);
