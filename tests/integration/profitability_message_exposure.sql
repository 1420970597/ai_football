CREATE TABLE v2_expert_signals (id integer PRIMARY KEY, message_id integer, fixture_id text);
CREATE TABLE v2_bet_orders (
    id integer PRIMARY KEY, signal_id integer, account_id integer, provider_identity_hash text,
    provider_order_no text, manual boolean, provider_placed_at timestamptz, provider_stake numeric
);
CREATE TABLE v2_order_receipt_evidence (id integer PRIMARY KEY, order_id integer, observed_at timestamptz, payload jsonb);

INSERT INTO v2_expert_signals VALUES (1,1,'leyu:1'), (2,1,'leyu:2'), (3,2,'leyu:3'), (4,3,'leyu:4');
INSERT INTO v2_bet_orders
SELECT id,id,1,'identity','receipt-' || id,id=4,:'begin'::timestamptz+id*interval '1 second',5
FROM v2_expert_signals;
INSERT INTO v2_order_receipt_evidence
SELECT id,id,provider_placed_at+interval '1 second',jsonb_build_object('order_no',provider_order_no,'state','accepted')
FROM v2_bet_orders;
INSERT INTO v2_order_receipt_evidence VALUES
    (11,1,:'begin'::timestamptz+interval '10 seconds','{"order_no":"receipt-1","state":"settled","net_profit":"-5","fees":"0","currency":"CNY"}'),
    (12,2,:'begin'::timestamptz+interval '20 seconds','{"order_no":"receipt-2","state":"settled","net_profit":"8"}'),
    (13,3,:'begin'::timestamptz+interval '10 seconds','{"order_no":"receipt-3","state":"settled","net_profit":"NaN"}');

\set observed '2026-11-19T00:00:15Z'
\ir ../../infra/profitability/message_exposure.sql
\set observed '2026-11-19T00:00:25Z'
\ir ../../infra/profitability/message_exposure.sql
DELETE FROM v2_order_receipt_evidence WHERE order_id=3;
\ir ../../infra/profitability/message_exposure.sql
