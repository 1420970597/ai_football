CREATE TABLE v2_expert_messages (id integer PRIMARY KEY, posted_at timestamptz);
CREATE TABLE v2_expert_signals (
    id integer PRIMARY KEY, message_id integer, fixture_id text, phase text,
    score_baseline jsonb, created_at timestamptz
);
CREATE TABLE v2_bet_orders (
    id integer PRIMARY KEY, signal_id integer, attempt_id integer, manual boolean,
    account_id integer, provider_identity_hash text, provider_order_no text, provider_stake numeric,
    provider_placed_at timestamptz, created_at timestamptz
);
CREATE TABLE v2_quote_evidence (
    id integer PRIMARY KEY, reference_id integer, evidence_kind text, evidence jsonb, captured_at timestamptz
);
CREATE TABLE v2_order_receipt_evidence (
    id integer PRIMARY KEY, order_id integer, payload jsonb, observed_at timestamptz
);

INSERT INTO v2_expert_messages
SELECT n, (CASE WHEN n < 7 THEN :'begin' ELSE :'split' END)::timestamptz
FROM generate_series(1,9) n;

INSERT INTO v2_expert_signals
SELECT id, id, 'fixture-' || id, CASE WHEN id=4 THEN 'scheduled' ELSE 'live' END,
       CASE WHEN id IN (4,5) THEN NULL ELSE '{"home":0,"away":0}'::jsonb END,
       posted_at + interval '10 seconds'
FROM v2_expert_messages;

INSERT INTO v2_bet_orders
SELECT id, id, id, false, 1, 'binding', 'native-' || id, 5, created_at + interval '15 seconds', created_at
FROM v2_expert_signals;

INSERT INTO v2_quote_evidence
SELECT id, id, 'order_attempt', jsonb_build_object('stage','submission_authorized',
    'attempt_id',CASE WHEN id=6 THEN 'incorrect-attempt' ELSE id::text END, 'signal_id',id::text,
    'score_baseline',CASE WHEN id IN (2,4,8) THEN '{"home":null,"away":0}'::jsonb
        WHEN id=3 THEN '{"home":1,"away":0}'::jsonb ELSE '{"home":0,"away":0}'::jsonb END),
    created_at + CASE WHEN id=7 THEN interval '16 seconds' ELSE interval '5 seconds' END
FROM v2_expert_signals;

INSERT INTO v2_order_receipt_evidence
SELECT id, id, jsonb_build_object('state','settled','order_no',provider_order_no,
    'net_profit',CASE WHEN id IN (1,3,9) THEN 4 WHEN id=6 THEN 0 ELSE -5 END),
    :'observed'::timestamptz + CASE WHEN id=8 THEN interval '1 second' ELSE interval '-1 second' END
FROM v2_bet_orders;
