-- Apply only to the dedicated LOGIN role whose credentials are provisioned separately.
ALTER ROLE v2_profitability_watch SET default_transaction_read_only = on;
ALTER ROLE v2_profitability_watch SET statement_timeout = '8s';
GRANT USAGE ON SCHEMA public TO v2_profitability_watch;
GRANT SELECT (
    id, account_id, state, manual, stake, provider_stake, reserved_stake, settled_net,
    provider_payout, provider_currency, provider_fees, pnl_basis, settled_at, receipt_observed_at,
    provider_placed_at, submitted_at, created_at, signal_id
) ON v2_bet_orders TO v2_profitability_watch;
GRANT SELECT (id, fixture_id, phase, market_kind, period, selection)
    ON v2_expert_signals TO v2_profitability_watch;
GRANT SELECT (id, sport) ON v2_fixtures TO v2_profitability_watch;
