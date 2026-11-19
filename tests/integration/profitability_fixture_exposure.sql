CREATE TABLE v2_expert_signals (
    id integer PRIMARY KEY, fixture_id text, phase text, market_id text, market_kind text,
    period text, score_basis text, selection text, line numeric
);
CREATE TABLE v2_fixtures (id text PRIMARY KEY, sport text NOT NULL);
INSERT INTO v2_fixtures VALUES ('leyu:1','football'), ('leyu:2','football');
CREATE TABLE v2_bet_orders (
    id integer PRIMARY KEY, signal_id integer, account_id integer, provider_identity_hash text,
    provider_order_no text, manual boolean, provider_placed_at timestamptz, created_at timestamptz, provider_stake numeric
);
CREATE TABLE v2_order_receipt_evidence (id integer PRIMARY KEY, order_id integer, observed_at timestamptz, payload jsonb);

CREATE TEMP TABLE orders (
    id integer, placed_seconds integer, settled_seconds integer, net numeric
);
INSERT INTO orders VALUES
    (1,-60,240,2), (2,60,400,-5), (3,120,400,2), (4,180,400,3), (5,240,400,-5),
    (6,255,420,4), (7,300,500,-5), (8,480,600,3), (9,100,400,2), (10,100,400,4),
    (11,130,400,1), (12,140,400,-1), (13,86430,86500,5), (14,145,400,-4), (15,150,NULL,NULL);

INSERT INTO v2_expert_signals
SELECT id, CASE WHEN id IN (12,14,15) THEN 'leyu:2' ELSE 'leyu:1' END,
       CASE WHEN id=9 THEN 'live' ELSE 'scheduled' END,
       CASE WHEN id=4 THEN '18' ELSE '2' END,'total', CASE WHEN id=4 THEN 'first_half' ELSE 'full_time' END,
       CASE WHEN id=11 THEN 'remaining_period' WHEN id=9 THEN 'unknown' ELSE 'full_period' END,
       CASE WHEN id=3 THEN 'under' WHEN id=10 THEN NULL ELSE 'over' END,
       CASE WHEN id=8 THEN 3.5 ELSE 2.5 END
FROM orders;

INSERT INTO v2_bet_orders
SELECT id,id,1,'account','native-' || id,id=6,
       :'begin'::timestamptz + placed_seconds*interval '1 second',
       :'begin'::timestamptz + placed_seconds*interval '1 second',5
FROM orders;

INSERT INTO v2_order_receipt_evidence
SELECT o.id,o.id,provider_placed_at + interval '1 second',
       jsonb_build_object('order_no',provider_order_no,'state','accepted')
FROM v2_bet_orders o
UNION ALL
SELECT o.id+100,o.id,:'begin'::timestamptz + settled_seconds*interval '1 second',
       jsonb_build_object('order_no',provider_order_no,'state','settled','net_profit',net)
FROM v2_bet_orders o JOIN orders s ON s.id=o.id WHERE s.settled_seconds IS NOT NULL;

\ir ../../infra/profitability/fixture_exposure.sql
