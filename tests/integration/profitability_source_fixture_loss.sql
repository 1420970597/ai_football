CREATE TABLE v2_expert_messages (id integer PRIMARY KEY, source_id integer);
CREATE TABLE v2_expert_signals (id integer PRIMARY KEY, message_id integer, expert_id integer, fixture_id text, phase text);
CREATE TABLE v2_bet_orders (
    id integer PRIMARY KEY, signal_id integer, account_id integer, provider_identity_hash text,
    provider_placed_at timestamptz, created_at timestamptz, provider_stake numeric,
    provider_order_no text, manual boolean
);
CREATE TABLE v2_order_receipt_evidence (id integer PRIMARY KEY, order_id integer, observed_at timestamptz, payload jsonb);

INSERT INTO v2_expert_messages VALUES (1,1),(2,2);
INSERT INTO v2_expert_signals
SELECT n, CASE WHEN n<=24 THEN 1 ELSE 2 END, CASE WHEN n<=24 THEN 1 ELSE 2 END,
       CASE WHEN n BETWEEN 21 AND 23 THEN 'fixture-1' ELSE 'fixture-' || n END,
       CASE WHEN n=45 THEN 'scheduled' ELSE 'live' END
FROM generate_series(1,46) n;

INSERT INTO v2_bet_orders
SELECT id,id,1,'account',placed,placed,5,'native-' || id,false
FROM v2_expert_signals
CROSS JOIN LATERAL (SELECT CASE WHEN id IN (24,45,46) THEN :'split'::timestamptz + interval '1 minute'
    ELSE :'begin'::timestamptz + (id%3)*interval '1 day' + interval '1 minute' END AS placed) t;

INSERT INTO v2_order_receipt_evidence
SELECT id,id,provider_placed_at + interval '1 hour',
       jsonb_build_object('order_no',provider_order_no,'state','settled','stake','5',
           'net_profit',CASE WHEN id=1 OR id BETWEEN 21 AND 23 THEN -5
                             WHEN id<24 THEN 0.25 WHEN id=24 THEN 4
                             WHEN id<45 THEN -0.5 ELSE -5 END)
FROM v2_bet_orders WHERE id<>46;

\ir ../../infra/profitability/source_holdout.sql
