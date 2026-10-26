ALTER TABLE v2_leyu_accounts ADD COLUMN config_revision BIGINT NOT NULL DEFAULT 1;
ALTER TABLE v2_leyu_accounts ADD COLUMN binding_revision BIGINT NOT NULL DEFAULT 1;
ALTER TABLE v2_leyu_accounts ADD COLUMN provider_identity_hash TEXT;
ALTER TABLE v2_leyu_accounts ADD COLUMN encrypted_session TEXT;
ALTER TABLE v2_leyu_accounts ADD COLUMN available_balance NUMERIC(16,2);
ALTER TABLE v2_leyu_accounts ADD COLUMN balance_observed_at TIMESTAMPTZ;
ALTER TABLE v2_leyu_accounts ADD COLUMN renewal_state TEXT NOT NULL DEFAULT 'ready' CHECK(renewal_state IN('ready','running','unknown','rejected'));
ALTER TABLE v2_leyu_accounts ADD COLUMN renewal_attempt UUID;
ALTER TABLE v2_leyu_accounts ADD COLUMN renewal_lease_until TIMESTAMPTZ;
ALTER TABLE v2_leyu_accounts ADD COLUMN renewal_retry_after TIMESTAMPTZ;
CREATE UNIQUE INDEX v2_active_account_identity_unique ON v2_leyu_accounts(provider_identity_hash) WHERE health_state<>'disabled';
ALTER TABLE v2_user_bet_settings ADD COLUMN revision BIGINT NOT NULL DEFAULT 1;
ALTER TABLE v2_user_bet_settings ADD COLUMN consent_account_id UUID;
ALTER TABLE v2_user_bet_settings ADD COLUMN consent_binding_revision BIGINT;

CREATE TABLE v2_account_jobs(
 id UUID PRIMARY KEY, user_id UUID NOT NULL REFERENCES v2_users(id), account_id UUID NOT NULL REFERENCES v2_leyu_accounts(id),
 binding_revision BIGINT NOT NULL, kind TEXT NOT NULL CHECK(kind IN('verify','refresh','reconcile')),
 state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN('queued','running','succeeded','failed','unknown','rejected')),
 attempt_id UUID, config_revision BIGINT, lease_until TIMESTAMPTZ,
 last_error TEXT, details JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX v2_account_jobs_active_unique ON v2_account_jobs(account_id) WHERE state IN('queued','running');
CREATE INDEX v2_account_jobs_claim_idx ON v2_account_jobs(state,created_at);

ALTER TABLE v2_bet_orders ADD COLUMN account_id UUID REFERENCES v2_leyu_accounts(id);
ALTER TABLE v2_bet_orders ADD COLUMN binding_revision BIGINT;
ALTER TABLE v2_bet_orders ADD COLUMN config_revision BIGINT;
ALTER TABLE v2_bet_orders ADD COLUMN policy_revision BIGINT;
ALTER TABLE v2_bet_orders ADD COLUMN provider_identity_hash TEXT;
ALTER TABLE v2_bet_orders ADD COLUMN reserved_stake NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK(reserved_stake>=0);
ALTER TABLE v2_bet_orders ADD COLUMN quote_snapshot JSONB NOT NULL DEFAULT '{}';
ALTER TABLE v2_bet_orders ADD COLUMN attempt_id UUID;
ALTER TABLE v2_bet_orders ADD COLUMN lease_until TIMESTAMPTZ;
ALTER TABLE v2_bet_orders ADD COLUMN prepared_digest TEXT;
ALTER TABLE v2_bet_orders ADD COLUMN provider_order_no TEXT;
ALTER TABLE v2_bet_orders ADD COLUMN submitted_at TIMESTAMPTZ;
ALTER TABLE v2_bet_orders ADD COLUMN settled_net NUMERIC(16,5);
ALTER TABLE v2_bet_orders ADD COLUMN settled_at TIMESTAMPTZ;
ALTER TABLE v2_bet_orders ALTER COLUMN price TYPE NUMERIC(12,5);
CREATE UNIQUE INDEX v2_order_user_signal_unique ON v2_bet_orders(user_id,signal_id) WHERE account_id IS NOT NULL;
CREATE INDEX v2_order_exposure_idx ON v2_bet_orders(user_id,created_at) INCLUDE(stake,reserved_stake,state);
CREATE INDEX v2_order_claim_idx ON v2_bet_orders(state,created_at) WHERE account_id IS NOT NULL;
