CREATE TEMP TABLE v2_fixtures (id text PRIMARY KEY, sport text, competition_id text, competition_name text);
CREATE TEMP TABLE v2_expert_signals (
    id integer PRIMARY KEY, fixture_id text, phase text, market_kind text, period text,
    score_basis text, withdrawn_at timestamptz, created_at timestamptz
);
CREATE TEMP TABLE v2_bet_orders (
    id integer PRIMARY KEY, signal_id integer, account_id integer, provider_identity_hash text,
    provider_order_no text, manual boolean, state text, result_payload jsonb, attempt_id integer
);
CREATE TEMP TABLE v2_quote_evidence (evidence_kind text, reference_id integer, evidence jsonb);

INSERT INTO v2_fixtures VALUES ('match', 'football', 'league-1', 'League One');
INSERT INTO v2_expert_signals
SELECT n, 'match', 'live', 'total', 'full_time', 'full_period', NULL,
       current_setting('profitability_test.starts_at')::timestamptz
FROM generate_series(1,9) n;

INSERT INTO v2_quote_evidence VALUES ('expert_signal',1,'{"market_snapshot":{}}');

INSERT INTO v2_bet_orders VALUES
    (1,1,1,'bound','filled',false,'settled',NULL,NULL),
    (2,1,1,'bound',NULL,false,'cancelled','{"reason":"eligibility_changed","category":"ignored"}',NULL),
    (3,2,1,'bound',NULL,false,'cancelled','{"category":"selected_native_market_not_open"}',NULL),
    (4,3,1,'bound',NULL,true,'cancelled','{"reason":"emergency_stop"}',NULL),
    (5,4,1,'bound',NULL,false,'rejected','{"category":"unknown_payload_example"}',NULL),
    (6,5,1,'bound',NULL,false,'rejected','{"category":""}',NULL),
    (7,2,2,'other',NULL,false,'cancelled','{"reason":"emergency_stop"}',NULL),
    (8,6,1,'bound',NULL,false,'cancelled','{"reason":"reference_ineligible"}',10),
    (9,7,1,'bound',NULL,false,'cancelled','{"reason":"below_minimum_odds"}',10),
    (10,8,1,'bound',NULL,false,'cancelled','{"reason":"reference_ineligible"}',10),
    (11,9,2,'other',NULL,false,'cancelled','{"reason":"reference_ineligible"}',10);

INSERT INTO v2_quote_evidence VALUES
    ('order_attempt',8,'{"stage":"preflight_validated","attempt_id":"10","signal_id":"6","automatic_price_decision":{"mode":"enforce","status":"reference_ineligible","reasons":["reference_contract_missing","reference_contract_missing","private details"]}}'),
    ('order_attempt',8,'{"stage":"preflight_validated","attempt_id":"9","signal_id":"6","automatic_price_decision":{"mode":"enforce","status":"reference_ineligible","reasons":["settlement_and_cost_approval_missing"]}}'),
    ('order_attempt',9,'{"stage":"preflight_validated","attempt_id":"10","signal_id":"7","automatic_price_decision":{"mode":"enforce","status":"below_minimum_odds"}}'),
    ('order_attempt',10,'{"stage":"preflight_validated","attempt_id":"10","signal_id":"8","automatic_price_decision":{"mode":"observe","status":"reference_ineligible","reasons":["reference_contract_missing"]}}'),
    ('order_attempt',11,'{"stage":"preflight_validated","attempt_id":"10","signal_id":"9","automatic_price_decision":{"mode":"enforce","status":"reference_ineligible","reasons":["reference_contract_missing"]}}');
