CREATE TABLE v2_fixtures (id text PRIMARY KEY, sport text);
CREATE TABLE v2_expert_signals (id integer PRIMARY KEY, fixture_id text, phase text, market_kind text, period text, selection text);
CREATE TABLE v2_bet_orders (
    id integer PRIMARY KEY, account_id integer, state text, manual boolean, stake numeric,
    provider_stake numeric, reserved_stake numeric, settled_net numeric, provider_payout numeric,
    provider_currency text, provider_fees numeric, pnl_basis text, settled_at timestamptz,
    receipt_observed_at timestamptz, provider_placed_at timestamptz, submitted_at timestamptz,
    created_at timestamptz, signal_id integer, provider_identity_hash text, provider_order_no text
);
CREATE TABLE v2_order_receipt_evidence (id integer PRIMARY KEY, order_id integer, payload jsonb, observed_at timestamptz);

INSERT INTO v2_fixtures VALUES ('fixture-a', 'football'), ('fixture-b', 'basketball');
INSERT INTO v2_expert_signals VALUES (1, 'fixture-a', 'live', 'total', 'full_time', 'over'),
    (2, 'fixture-b', 'scheduled', 'handicap', 'full_time', 'away');
INSERT INTO v2_bet_orders
SELECT n, 10, 'settled', n % 2 = 0, 5, 5, 0, 2, 7, 'CNY', 0, 'provider_reported_net',
       (:'anchor'::timestamptz + n * interval '12 hours'),
       (:'anchor'::timestamptz + n * interval '12 hours' + interval '1 minute'),
       (:'anchor'::timestamptz + n * interval '12 hours' - interval '1 hour'),
       NULL, (:'anchor'::timestamptz + n * interval '12 hours' - interval '1 hour'), 1 + n % 2,
       'test-provider-account', 'test-order-' || n
FROM generate_series(0, 3) n;

INSERT INTO v2_order_receipt_evidence
SELECT id, id, jsonb_build_object('state', 'settled', 'order_no', provider_order_no,
    'pnl_basis', pnl_basis, 'currency', provider_currency, 'stake', provider_stake::text,
    'payout', provider_payout::text, 'net_profit', settled_net::text, 'fees', provider_fees::text,
    'settled_at', settled_at), receipt_observed_at
FROM v2_bet_orders;
