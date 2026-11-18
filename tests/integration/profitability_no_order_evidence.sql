CREATE TABLE v2_fixtures (id text PRIMARY KEY, sport text);
CREATE TABLE v2_leyu_accounts (id integer PRIMARY KEY, user_id integer, provider_identity_hash text);
CREATE TABLE v2_user_bet_settings (user_id integer PRIMARY KEY, auto_sport_scope text);
CREATE TABLE v2_expert_messages (id integer PRIMARY KEY, posted_at timestamptz, source_id integer DEFAULT 1);
CREATE TABLE v2_telegram_events (
    id integer PRIMARY KEY, source_id integer, message_id integer, event_kind text, received_at timestamptz
);
CREATE TABLE v2_quote_evidence (
    id integer PRIMARY KEY, reference_id integer, evidence_kind text, captured_at timestamptz, evidence jsonb
);
CREATE TABLE v2_analysis_jobs (id uuid PRIMARY KEY, message_id integer, created_at timestamptz);
CREATE TABLE v2_analysis_traces (job_id uuid, trace_kind text, created_at timestamptz, payload jsonb);
CREATE TABLE v2_expert_signals (
    id integer PRIMARY KEY, fixture_id text, market_id text, market_kind text, period text,
    selection text, phase text, score_basis text, line numeric, score_baseline jsonb, created_at timestamptz,
    message_id integer
);
CREATE TABLE v2_bet_orders (
    id integer PRIMARY KEY, signal_id integer, user_id integer, account_id integer, provider_identity_hash text,
    provider_order_no text, manual boolean, created_at timestamptz, state text
);
CREATE TABLE v2_audit_log (
    id integer PRIMARY KEY, action text, resource_type text, actor_id integer, resource_id text,
    details jsonb, created_at timestamptz
);

INSERT INTO v2_fixtures VALUES ('football-fixture','football'),('basketball-fixture','basketball');
INSERT INTO v2_leyu_accounts VALUES (1,1,'binding'),(2,2,'other');
INSERT INTO v2_user_bet_settings VALUES (1,'football'),(2,'both');
INSERT INTO v2_expert_signals
SELECT n, CASE WHEN n=4 THEN 'basketball-fixture' ELSE 'football-fixture' END,
       '2','total','full_time','over','live','unknown',2.5,
       CASE WHEN n=3 THEN '{"home":1,"away":0}'::jsonb ELSE '{"home":0,"away":0}'::jsonb END,
       '2026-01-01T00:00:00Z'::timestamptz + (n-2)*interval '1 minute',n
FROM generate_series(1,5) n;
INSERT INTO v2_bet_orders VALUES
    (1,1,1,1,'binding','native-1',false,'2025-12-31T23:59:30Z','settled'),
    (2,5,1,1,'binding',NULL,false,'2026-01-01T00:03:30Z','pending');
INSERT INTO v2_audit_log VALUES
    (1,'auto_betting_authorized','leyu_account',1,'1','{"auto_sport_scope":"football"}','2025-12-31T23:58:00Z');

INSERT INTO v2_expert_signals
SELECT n, 'football-fixture','2','total','full_time','over','live','unknown',line,
       '{"home":0,"away":0}',created_at::timestamptz,n
FROM (VALUES (6,3.5,'2026-01-01T01:00:00Z'),(7,3.5,'2026-01-01T01:00:04Z'),
             (8,4.5,'2026-01-01T02:00:00Z'),(9,4.5,'2026-01-01T02:00:04Z'),
             (10,5.5,'2026-01-01T03:00:00Z'),(11,5.5,'2026-01-01T03:00:04Z'),
             (12,6.5,'2026-01-01T04:00:00Z'),(13,6.5,'2026-01-01T04:00:04Z'),
             (14,7.5,'2026-01-01T05:00:00Z'),(15,7.5,'2026-01-01T05:00:04Z'),
             (16,8.5,'2026-01-01T06:00:00Z'),(17,8.5,'2026-01-01T06:00:04Z'),
             (18,9.5,'2026-01-01T07:00:00Z'),(19,9.5,'2026-01-01T07:00:04Z'),
             (20,10.5,'2026-01-01T07:30:00Z'),(21,11.5,'2026-01-01T07:30:04Z'),
             (22,12.5,'2026-01-01T07:45:00Z'),(23,13.5,'2026-01-01T07:55:00Z')) AS s(n,line,created_at);
INSERT INTO v2_expert_messages (id,posted_at)
SELECT message_id, created_at - interval '10 seconds' FROM v2_expert_signals;
UPDATE v2_expert_messages SET posted_at = posted_at - interval '291 seconds' WHERE id=20;
UPDATE v2_expert_messages SET posted_at = posted_at - interval '290 seconds' WHERE id=21;
UPDATE v2_expert_messages SET posted_at = posted_at - interval '300 seconds' WHERE id IN (22,23);
INSERT INTO v2_telegram_events VALUES
    (1,1,20,'message','2026-01-01T07:25:01Z'),
    (2,1,22,'edit','2026-01-01T07:44:59Z'),
    (3,1,23,'message','2026-01-01T07:50:00Z');
INSERT INTO v2_analysis_jobs VALUES
    ('00000000-0000-0000-0000-000000000020',20,'2026-01-01T07:25:02Z');
INSERT INTO v2_quote_evidence VALUES
    (1,20,'expert_signal','2026-01-01T07:30:00Z',
        '{"job_id":"00000000-0000-0000-0000-000000000020","attempt_id":"success"}'),
    (2,23,'expert_signal','2026-01-01T07:55:00Z','{"job_id":"malformed","attempt_id":"other"}');
INSERT INTO v2_analysis_traces (job_id,trace_kind,created_at,payload)
SELECT '00000000-0000-0000-0000-000000000020', trace_kind, created_at::timestamptz, payload::jsonb
FROM (VALUES
    ('attempt_claimed','2026-01-01T07:25:03Z','{"attempt_id":"first"}'),
    ('review_failure','2026-01-01T07:25:10Z','{"failure_category":"provider_transient","failure_stage":"model_review"}'),
    ('attempt_claimed','2026-01-01T07:29:59.5Z','{"attempt_id":"success"}'),
    ('hermes_request','2026-01-01T07:29:59.8Z','{"attempt_id":"success","message_id":"20"}')
) AS t(trace_kind,created_at,payload);
INSERT INTO v2_bet_orders VALUES
    (3,6,1,1,'binding','native-3',false,'2026-01-01T01:00:05Z','accepted'),
    (4,8,1,1,'binding','native-4',false,'2026-01-01T02:05:00Z','settled'),
    (5,11,1,1,'binding','native-5',false,'2026-01-01T03:00:05Z','settled'),
    (6,12,1,1,'binding',NULL,false,'2026-01-01T04:00:01Z','cancelled'),
    (7,12,1,1,'binding','native-7',false,'2026-01-01T04:00:05Z','accepted'),
    (8,14,1,1,'binding',NULL,false,'2026-01-01T05:00:01Z','unknown'),
    (9,16,1,1,'binding',NULL,false,'2026-01-01T06:00:01Z','rejected'),
    (10,18,1,1,'binding',' ',false,'2026-01-01T07:00:01Z','settled'),
    (11,16,2,2,'other',NULL,true,'2026-01-01T06:00:00Z','unknown');
