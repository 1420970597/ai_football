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
