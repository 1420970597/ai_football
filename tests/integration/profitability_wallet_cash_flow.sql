CREATE TABLE v2_bet_orders (
    account_id integer, provider_identity_hash text, provider_order_no text, state text,
    provider_stake numeric, provider_payout numeric, provider_placed_at timestamptz,
    settled_at timestamptz, submitted_at timestamptz, created_at timestamptz,
    receipt_observed_at timestamptz, settled_net numeric, pnl_basis text
);

INSERT INTO v2_bet_orders (account_id, provider_identity_hash, provider_order_no, state,
    provider_stake, provider_payout, provider_placed_at, settled_at, settled_net, pnl_basis)
VALUES (1, 'test-account', 'placed-before-span', 'settled', 5, 9, '2026-11-18T02:19:00Z', '2026-11-18T02:22:00Z', 3, 'provider_reported_net'),
       (1, 'test-account', 'placed-and-settled', 'settled', 7, 0, '2026-11-18T02:23:00Z', '2026-11-18T02:26:00Z', -6, 'provider_reported_net'),
       (1, 'test-account', 'settled-after-span', 'settled', 10, 14, '2026-11-18T02:27:00Z', '2026-11-18T02:31:00Z', 4, 'provider_reported_net');

SELECT replace(encode(convert_to('[
    {"sampled_at":"2026-11-18T02:20:01Z","wallet":{"observed_at":"2026-11-18T02:20:00Z","available_balance":100}},
    {"sampled_at":"2026-11-18T02:25:01Z","wallet":{"observed_at":"2026-11-18T02:25:00Z","available_balance":102}},
    {"sampled_at":"2026-11-18T02:30:01Z","wallet":{"observed_at":"2026-11-18T02:30:00Z","available_balance":92}}
]', 'UTF8'), 'base64'), chr(10), '') AS wallet_samples \gset
\set window_start 2026-11-18T02:14:09.845374Z
\set window_end 2026-11-18T14:14:09.845374Z
\ir ../../infra/profitability/wallet_cash_flow.sql

UPDATE v2_bet_orders SET provider_stake = NULL WHERE provider_order_no = 'placed-before-span';
\ir ../../infra/profitability/wallet_cash_flow.sql

UPDATE v2_bet_orders SET settled_net = provider_payout - provider_stake, pnl_basis = 'gross_return_minus_stake';
\ir ../../infra/profitability/wallet_cash_flow.sql

TRUNCATE v2_bet_orders;
INSERT INTO v2_bet_orders (account_id, provider_identity_hash, provider_order_no, state,
    provider_stake, provider_payout, provider_placed_at, settled_at, created_at)
VALUES (1, 'test-account', 'first-sample', 'settled', 5, 5, '2026-11-18T02:20:00Z', '2026-11-18T02:21:00Z', '2026-11-18T02:19:00Z'),
       (1, 'test-account', 'shared-sample', 'settled', 7, 0, '2026-11-18T02:25:00Z', '2026-11-18T02:27:00Z', '2026-11-18T02:24:00Z');
\ir ../../infra/profitability/wallet_cash_flow.sql

INSERT INTO v2_bet_orders (account_id, provider_identity_hash, provider_order_no, state,
    provider_stake, provider_placed_at, created_at)
VALUES (1, 'test-account', 'known-pending', 'accepted', 5, '2026-11-18T02:22:00Z', '2026-11-18T02:21:00Z'),
       (1, 'test-account', NULL, 'unknown', NULL, NULL, '2026-11-18T02:26:00Z');
\ir ../../infra/profitability/wallet_cash_flow.sql

\set wallet_samples W10=
\ir ../../infra/profitability/wallet_cash_flow.sql
