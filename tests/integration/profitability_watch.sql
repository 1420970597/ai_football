CREATE TABLE v2_fixtures (id text PRIMARY KEY, sport text);
CREATE TABLE v2_expert_signals (id integer PRIMARY KEY, fixture_id text, phase text, market_kind text, period text, selection text);
CREATE TABLE v2_bet_orders (
    id integer PRIMARY KEY, account_id integer, state text, manual boolean, stake numeric,
    provider_stake numeric, reserved_stake numeric, settled_net numeric, provider_payout numeric,
    provider_currency text, provider_fees numeric, pnl_basis text, settled_at timestamptz,
    receipt_observed_at timestamptz, provider_placed_at timestamptz, submitted_at timestamptz,
    created_at timestamptz, signal_id integer
);

INSERT INTO v2_fixtures VALUES ('fixture-a', 'football'), ('fixture-b', 'basketball');
INSERT INTO v2_expert_signals VALUES (1, 'fixture-a', 'live', 'total', 'full_time', 'over'),
    (2, 'fixture-b', 'scheduled', 'handicap', 'full_time', 'away');
INSERT INTO v2_bet_orders
SELECT n, 10, 'settled', n % 2 = 0, 5, 5, 0, 2, 7, 'CNY', 0, 'provider_reported_net',
       (:'anchor'::timestamptz + n * interval '12 hours'),
       (:'anchor'::timestamptz + n * interval '12 hours' + interval '1 minute'),
       (:'anchor'::timestamptz + n * interval '12 hours' - interval '1 hour'),
       NULL, (:'anchor'::timestamptz + n * interval '12 hours' - interval '1 hour'), 1 + n % 2
FROM generate_series(0, 3) n;
