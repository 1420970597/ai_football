CREATE TABLE v2_fixtures (id text PRIMARY KEY, sport text);
CREATE TABLE v2_expert_signals (
    id integer PRIMARY KEY, fixture_id text, phase text, market_kind text, market_id text, period text,
    line numeric, selection text, score_basis text, score_baseline jsonb, created_at timestamptz,
    quote_time timestamptz, recommendation_price numeric
);
CREATE TABLE v2_bet_orders (
    id integer PRIMARY KEY, attempt_id integer, signal_id integer, provider_order_no text,
    provider_placed_at timestamptz, provider_price numeric, settled_net numeric, provider_fees numeric,
    provider_currency text, account_id integer, provider_identity_hash text, state text, provider_stake numeric,
    manual boolean NOT NULL DEFAULT false
);
CREATE TABLE v2_quote_evidence (
    id integer PRIMARY KEY, reference_id integer, fixture_id text, market_id text, period text,
    selection text, line numeric, price numeric, evidence jsonb, score_baseline jsonb,
    evidence_kind text, captured_at timestamptz, source_updated_at timestamptz
);
CREATE TABLE v2_order_receipt_evidence (id integer PRIMARY KEY, order_id integer, observed_at timestamptz, payload jsonb);

INSERT INTO v2_fixtures VALUES ('leyu:123', 'football'), ('other:123', 'football');
INSERT INTO v2_expert_signals
SELECT n, CASE WHEN n=5 THEN 'other:123' ELSE 'leyu:123' END, 'live', 'total',
    CASE WHEN n=4 THEN '99' WHEN n=6 THEN '2' ELSE '18' END,
    CASE WHEN n=6 THEN 'full_time' ELSE 'first_half' END,
    1.5, 'over', CASE WHEN n=7 THEN 'remaining_period' ELSE 'unknown' END,
    CASE WHEN n=2 THEN '{"home":null,"away":0}' ELSE '{"home":1,"away":0}' END::jsonb,
    :'begin'::timestamptz, :'begin'::timestamptz, 2.00
FROM generate_series(1,12) n;

INSERT INTO v2_bet_orders
SELECT id, id, id, id::text, created_at + interval '10 seconds', 2.10, 5.50, NULL, NULL, 1, 'same', 'settled', 5.00,
       id=6
FROM v2_expert_signals;

INSERT INTO v2_quote_evidence
SELECT id, id, fixture_id, market_id, period, selection, line, recommendation_price,
    '{"market_snapshot":{"market_instance_id":"market","play_id":"play","options":[
        {"selection":"over","selection_id":"over-id","price":"2.00"},
        {"selection":"under","selection_id":"under-id","price":"1.80"}
    ]}}'::jsonb, score_baseline, 'expert_signal', created_at, quote_time
FROM v2_expert_signals;

INSERT INTO v2_quote_evidence
SELECT id+100, id, fixture_id, market_id, period, selection, line, 2.10,
    jsonb_build_object('stage','preflight_validated','attempt_id',id::text,'signal_id',id::text,
       'phase',phase,'market_kind',market_kind,'score_basis',score_basis,'market_instance_id','market',
       'play_id','play','selection_id','over-id','score_baseline',
       CASE WHEN id=3 THEN '{"home":2,"away":0}'::jsonb ELSE score_baseline END,
       'score_benchmark',CASE WHEN id=3 THEN '2:0' ELSE '1:0' END),
    score_baseline, 'order_attempt', created_at + interval '5 seconds', quote_time
FROM v2_expert_signals;

DELETE FROM v2_quote_evidence WHERE id IN (8,109);
UPDATE v2_quote_evidence SET evidence = jsonb_set(evidence, '{market_snapshot,options}', '[]') WHERE id = 10;
UPDATE v2_quote_evidence SET evidence = jsonb_set(evidence, '{selection_id}', '"other"') WHERE id = 111;
UPDATE v2_bet_orders SET provider_price = NULL WHERE id = 12;

ALTER TABLE v2_bet_orders ADD COLUMN provider_payout numeric;
ALTER TABLE v2_bet_orders ADD COLUMN settled_at timestamptz;
ALTER TABLE v2_expert_signals ADD COLUMN outcome text;
ALTER TABLE v2_expert_signals ADD COLUMN settled_at timestamptz;
ALTER TABLE v2_expert_signals ADD COLUMN unit_net numeric;
ALTER TABLE v2_expert_signals ADD COLUMN effective_stake numeric;
ALTER TABLE v2_expert_signals ADD COLUMN result_version integer;
ALTER TABLE v2_expert_signals ADD COLUMN settlement_reason text;

CREATE TABLE v2_fixture_results (fixture_id text, version integer, status text);
CREATE TABLE v2_signal_settlements (
    signal_id integer, fixture_id text, result_version integer, outcome text,
    unit_net numeric, effective_stake numeric, reason text
);

INSERT INTO v2_fixture_results VALUES ('leyu:123', 1, 'confirmed');
UPDATE v2_expert_signals
SET outcome = CASE WHEN id=1 THEN 'win' ELSE 'half_win' END,
    effective_stake = CASE WHEN id=1 THEN 1 ELSE 0.5 END,
    unit_net = CASE WHEN id=1 THEN 1 ELSE 0.5 END,
    result_version = 1, settlement_reason = 'confirmed_test', settled_at = created_at + interval '3 hours'
WHERE id IN (1,6);

INSERT INTO v2_order_receipt_evidence
VALUES (1, 1, :'begin'::timestamptz + interval '15 seconds',
        '{"order_no":"1","state":"settled","stake":"5.00","payout":"5","net_profit":"0",
          "provider_settlement_codes":{"betResult":0,"betStatus":5,"orderStatus":4,"cancelType":41}}');
INSERT INTO v2_signal_settlements
SELECT id,fixture_id,result_version,outcome,unit_net,effective_stake,settlement_reason
FROM v2_expert_signals WHERE id IN (1,6);
UPDATE v2_quote_evidence q
SET evidence=q.evidence || jsonb_build_object('market_kind',s.market_kind,'score_basis',s.score_basis)
FROM v2_expert_signals s WHERE q.id=s.id AND s.id IN (1,6);
UPDATE v2_bet_orders
SET provider_payout=CASE WHEN id=1 THEN 10.50 ELSE 7.75 END,
    settled_net=CASE WHEN id=1 THEN 5.50 ELSE 2.75 END
WHERE id IN (1,6);
