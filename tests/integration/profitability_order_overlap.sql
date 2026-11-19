CREATE TEMP TABLE v2_fixtures (id text PRIMARY KEY, sport text);
CREATE TEMP TABLE v2_expert_signals (
    id integer PRIMARY KEY, fixture_id text, market_id text, market_kind text, period text, phase text,
    selection text, line numeric, score_basis text, score_baseline jsonb
);
CREATE TEMP TABLE v2_bet_orders (
    id integer PRIMARY KEY, signal_id integer, user_id integer, account_id integer, provider_identity_hash text,
    provider_order_no text, manual boolean, provider_placed_at timestamptz, created_at timestamptz
);
CREATE TEMP TABLE v2_order_receipt_evidence (order_id integer, observed_at timestamptz, payload jsonb);

INSERT INTO v2_fixtures SELECT 'leyu:' || n, 'football' FROM generate_series(1,4) n;
INSERT INTO v2_expert_signals VALUES
    (1,'leyu:1','2','total','full_time','scheduled','over',2.5,'unknown',NULL),
    (2,'leyu:1','2','total','full_time','live','over',2.5,'unknown','{"home":1,"away":0}'),
    (3,'leyu:1','2','total','full_time','live','over',2.75,'unknown','{"home":1,"away":0}'),
    (4,'leyu:1','2','total','full_time','live','over',3.25,'full_period','{"home":2,"away":0}'),
    (5,'leyu:1','2','total','full_time','live','over',3.75,'full_period','{"home":2,"away":0}'),
    (6,'leyu:1','4','handicap','full_time','live','home',-0.5,'unknown','{"home_score":1,"away_score":0}'),
    (7,'leyu:1','4','handicap','full_time','live','home',-0.5,'unknown','{"home":2,"away":0}'),
    (8,'leyu:1','4','handicap','full_time','live','home',-0.5,'remaining_period','{"home":null,"away":0}'),
    (9,'leyu:2','1','winner','full_time','scheduled','home',NULL,'full_period',NULL),
    (10,'leyu:2','1','winner','full_time','live','home',0,'full_period',NULL),
    (11,'leyu:3','2','total','full_time','scheduled','over',2.5,'unknown',NULL),
    (12,'leyu:3','2','total','full_time','live','over',2.5,'unknown',NULL),
    (13,'leyu:4','2','total','full_time','scheduled','over',2.5,'unknown',NULL),
    (14,'leyu:1','2','total','full_time','live','over',2.5,'full_period',NULL),
    (15,'leyu:4','2','total','full_time','live','over',2.5,NULL,NULL),
    (16,'leyu:1','2','total','full_time','live','under',2.5,'full_period',NULL),
    (17,'leyu:1','18','total','first_half','live','over',2.5,'unknown',NULL),
    (18,'leyu:1','2','total','full_time','live','over',2.5,'full_period',NULL);

INSERT INTO v2_bet_orders
SELECT id,id,1,CASE WHEN id=14 THEN 2 ELSE 1 END,'identity',
       CASE WHEN id=12 THEN '11' ELSE id::text END,id=4,
       CASE WHEN id<>13 THEN current_setting('profitability_test.starts_at')::timestamptz + make_interval(mins=>CASE
           WHEN id=1 THEN -1 WHEN id=10 THEN 8 ELSE id-1 END) END,
       current_setting('profitability_test.starts_at')::timestamptz + make_interval(mins=>id-2)
FROM v2_expert_signals;
INSERT INTO v2_order_receipt_evidence
SELECT order_id,current_setting('profitability_test.starts_at')::timestamptz + lag,
       jsonb_build_object('state','settled','order_no',order_no)
FROM (VALUES (1,interval '3 minutes','1'), (2,interval '3 minutes','2'), (3,interval '3 minutes','3'),
             (4,interval '2 minutes','wrong'), (4,interval '3 minutes 59 seconds','4'))
     AS evidence(order_id,lag,order_no);
