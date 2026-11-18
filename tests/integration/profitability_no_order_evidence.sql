CREATE TABLE v2_fixtures (id text PRIMARY KEY, sport text);
CREATE TABLE v2_leyu_accounts (id integer PRIMARY KEY, user_id integer, provider_identity_hash text);
CREATE TABLE v2_user_bet_settings (user_id integer PRIMARY KEY, auto_sport_scope text);
CREATE TABLE v2_expert_signals (
    id integer PRIMARY KEY, fixture_id text, market_id text, market_kind text, period text,
    selection text, phase text, score_basis text, line numeric, score_baseline jsonb, created_at timestamptz
);
CREATE TABLE v2_bet_orders (
    id integer PRIMARY KEY, signal_id integer, user_id integer, account_id integer, provider_identity_hash text,
    provider_order_no text, manual boolean, created_at timestamptz
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
       '2026-01-01T00:00:00Z'::timestamptz + (n-2)*interval '1 minute'
FROM generate_series(1,5) n;
INSERT INTO v2_bet_orders VALUES
    (1,1,1,1,'binding','native-1',false,'2025-12-31T23:59:30Z'),
    (2,5,1,1,'binding',NULL,false,'2026-01-01T00:03:30Z');
INSERT INTO v2_audit_log VALUES
    (1,'auto_betting_authorized','leyu_account',1,'1','{"auto_sport_scope":"football"}','2025-12-31T23:58:00Z');
