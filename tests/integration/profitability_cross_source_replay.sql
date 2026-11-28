CREATE TABLE v2_expert_messages (
    id integer PRIMARY KEY, source_id integer, posted_at timestamptz, deleted_at timestamptz
);
CREATE TABLE v2_expert_signals (
    id integer PRIMARY KEY, message_id integer, expert_id integer, fixture_id text, phase text,
    market_id text, market_kind text, period text, line numeric, selection text, score_basis text,
    score_baseline jsonb, created_at timestamptz, withdrawn_at timestamptz
);
CREATE TABLE v2_bet_orders (
    id integer PRIMARY KEY, signal_id integer, attempt_id integer, manual boolean, account_id integer,
    provider_identity_hash text, provider_order_no text, provider_stake numeric, provider_placed_at timestamptz
);
CREATE TABLE v2_quote_evidence (
    id integer PRIMARY KEY, reference_id integer, evidence_kind text, evidence jsonb, captured_at timestamptz
);
CREATE TABLE v2_order_receipt_evidence (
    id integer PRIMARY KEY, order_id integer, payload jsonb, observed_at timestamptz
);

INSERT INTO v2_expert_messages
SELECT n, 1, (CASE WHEN n<4 THEN :'begin' ELSE :'split' END)::timestamptz FROM generate_series(1,6) n;

INSERT INTO v2_expert_signals
SELECT id,id,1,'match-' || id, CASE WHEN id=3 THEN 'scheduled' ELSE 'live' END,
       '2','total','full_time',2.5,'over','full_period','{"home":0,"away":0}',posted_at + interval '1 minute',NULL
FROM v2_expert_messages;

INSERT INTO v2_bet_orders
SELECT id,id,id,false,1,'account','native-' || id,5,created_at + interval '30 seconds' FROM v2_expert_signals;

INSERT INTO v2_quote_evidence
SELECT id,id,'order_attempt',jsonb_build_object('stage','submission_authorized',
    'attempt_id',CASE WHEN id=5 THEN 'wrong-attempt' ELSE id::text END,'signal_id',id::text,
    'score_baseline',CASE WHEN id=6 THEN '{"home":1,"away":0}'::jsonb ELSE score_baseline END),
    created_at + interval '20 seconds'
FROM v2_expert_signals;

INSERT INTO v2_order_receipt_evidence
SELECT id,id,jsonb_build_object('order_no',provider_order_no,'state','settled',
    'net_profit',CASE WHEN id=1 OR id=5 THEN 4 ELSE -5 END),
    :'observed'::timestamptz + CASE WHEN id=4 THEN interval '1 second' ELSE interval '-1 second' END
FROM v2_bet_orders;

CREATE TEMP TABLE peers (id integer, compared_to integer, direction text, issue text);
INSERT INTO peers VALUES
    (11,1,'over','none'), (12,1,'over','none'), (13,2,'under','none'),
    (14,2,'over','wrong_score'), (15,2,'over','same_expert'), (16,2,'over','same_source'),
    (17,2,'over','too_late'), (18,2,'over','withdrawn'), (19,2,'over','wrong_line'),
    (20,2,'over','too_old'), (21,2,'over','wrong_period'), (22,2,'over','deleted'),
    (23,3,'over','none'), (24,3,'under','deleted_after_send'), (25,4,'over','too_late');

INSERT INTO v2_expert_messages
SELECT p.id,CASE WHEN issue='same_source' THEN m.source_id ELSE 2 END,
       m.posted_at - CASE WHEN issue='too_old' THEN interval '10 minutes' ELSE interval '0' END,
       CASE WHEN issue='deleted' THEN m.posted_at + interval '1 minute 10 seconds'
            WHEN issue='deleted_after_send' THEN m.posted_at + interval '1 day' END
FROM peers p JOIN v2_expert_messages m ON m.id=p.compared_to;

INSERT INTO v2_expert_signals
SELECT p.id,p.id,CASE WHEN issue='same_expert' THEN s.expert_id ELSE 2 END,s.fixture_id,s.phase,
       s.market_id,s.market_kind,CASE WHEN issue='wrong_period' THEN 'first_half' ELSE s.period END,
       CASE WHEN issue='wrong_line' THEN 3.5 ELSE s.line END,p.direction,s.score_basis,
       CASE WHEN issue='wrong_score' THEN '{"home":2,"away":0}'::jsonb ELSE s.score_baseline END,
       s.created_at + CASE WHEN issue='too_late' THEN interval '20 seconds' ELSE interval '10 seconds' END,
       CASE WHEN issue='withdrawn' THEN s.created_at + interval '15 seconds' END
FROM peers p JOIN v2_expert_signals s ON s.id=p.compared_to;
