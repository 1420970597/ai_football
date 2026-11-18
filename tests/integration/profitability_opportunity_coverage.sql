CREATE TABLE v2_fixtures (id text PRIMARY KEY, sport text, competition_id text, competition_name text);
CREATE TABLE v2_expert_signals (
    id integer PRIMARY KEY, fixture_id text, phase text, market_kind text, period text,
    score_basis text, withdrawn_at timestamptz, created_at timestamptz
);
CREATE TABLE v2_bet_orders (
    id integer PRIMARY KEY, signal_id integer, account_id integer, provider_identity_hash text,
    provider_order_no text, manual boolean, state text, result_payload jsonb
);
CREATE TABLE v2_quote_evidence (evidence_kind text, reference_id integer, evidence jsonb);

INSERT INTO v2_fixtures VALUES ('match', 'football', 'league-1', 'League One');
INSERT INTO v2_expert_signals
SELECT n, 'match', 'live', 'total', 'full_time', 'full_period', NULL, :'begin'::timestamptz
FROM generate_series(1,5) n;

INSERT INTO v2_quote_evidence VALUES ('expert_signal',1,'{"market_snapshot":{}}');

INSERT INTO v2_bet_orders VALUES
    (1,1,1,'bound','filled',false,'settled',NULL),
    (2,1,1,'bound',NULL,false,'cancelled','{"reason":"eligibility_changed","category":"ignored"}'),
    (3,2,1,'bound',NULL,false,'cancelled','{"category":"selected_native_market_not_open"}'),
    (4,3,1,'bound',NULL,true,'cancelled','{"reason":"emergency_stop"}'),
    (5,4,1,'bound',NULL,false,'rejected','{"category":"unknown_payload_example"}'),
    (6,5,1,'bound',NULL,false,'rejected','{"category":""}'),
    (7,2,2,'other',NULL,false,'cancelled','{"reason":"emergency_stop"}');
