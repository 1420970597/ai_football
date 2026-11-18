CREATE TABLE v2_fixtures (id text PRIMARY KEY, sport text);
CREATE TABLE v2_expert_signals (
    id integer PRIMARY KEY, fixture_id text, phase text, market_kind text, market_id text, period text,
    line numeric, selection text, score_basis text, score_baseline jsonb, created_at timestamptz,
    recommendation_price numeric, outcome text, unit_net numeric, result_version integer
);
CREATE TABLE v2_bet_orders (
    id integer PRIMARY KEY, attempt_id integer, signal_id integer, settled_at timestamptz,
    receipt_observed_at timestamptz, provider_placed_at timestamptz, provider_stake numeric,
    provider_price numeric, settled_net numeric, state text, manual boolean
);
CREATE TABLE v2_quote_evidence (
    id integer PRIMARY KEY, reference_id integer, fixture_id text, market_id text, period text,
    selection text, line numeric, price numeric, evidence jsonb, evidence_kind text, captured_at timestamptz
);
CREATE TABLE v2_signal_settlements (
    signal_id integer, fixture_id text, result_version integer, outcome text, unit_net numeric,
    calculation jsonb, created_at timestamptz, algorithm_version text
);

INSERT INTO v2_fixtures VALUES ('leyu:123', 'football');
INSERT INTO v2_expert_signals
SELECT n, 'leyu:123', 'live', CASE WHEN n IN (2,3) THEN 'handicap' ELSE 'total' END,
    CASE WHEN n IN (2,3) THEN '4' WHEN n=5 THEN 'other' ELSE '2' END,
    'full_time', 1.5, CASE WHEN n IN (2,3) THEN 'home' ELSE 'over' END,
    'unknown', '{"home":1,"away":0}', :'anchor'::timestamptz, 2.00,
    CASE n WHEN 2 THEN 'half_win' WHEN 11 THEN 'pending' ELSE 'win' END,
    CASE WHEN n=2 THEN 0.50 ELSE 1.00 END, 2
FROM generate_series(1,12) n;
INSERT INTO v2_bet_orders
SELECT id, id, id, created_at + interval '8 hours', created_at + interval '8 hours',
    created_at + interval '10 seconds', 5, CASE WHEN id=12 THEN NULL ELSE 2.10 END,
    CASE id WHEN 1 THEN 5.49 WHEN 2 THEN 0 WHEN 6 THEN NULL ELSE 5.50 END, 'settled', false
FROM v2_expert_signals;
INSERT INTO v2_bet_orders
VALUES (13, 13, 13, :'anchor'::timestamptz + interval '8 hours',
    :'anchor'::timestamptz + interval '8 hours', :'anchor'::timestamptz + interval '10 seconds',
    5, 2.10, 5.50, 'settled', false);
INSERT INTO v2_quote_evidence
SELECT id, id, fixture_id, market_id, period, CASE WHEN id=9 THEN 'under' ELSE selection END, line,
    CASE id WHEN 1 THEN 1.90 WHEN 2 THEN 2.25 ELSE 2.10 END,
    jsonb_build_object('stage','preflight_validated','attempt_id',id::text,'signal_id',
       CASE WHEN id=7 THEN 'wrong' ELSE id::text END,'phase',phase,'market_kind',market_kind,
       'score_basis',CASE WHEN id=10 THEN 'full_period' ELSE score_basis END,
       'score_baseline',score_baseline,'score_benchmark',CASE WHEN id=3 THEN '2:0' ELSE '1:0' END),
    'order_attempt', created_at + CASE WHEN id=8 THEN interval '11 seconds' ELSE interval '5 seconds' END
FROM v2_expert_signals;
INSERT INTO v2_signal_settlements
SELECT id, fixture_id, CASE WHEN id=4 THEN 1 ELSE result_version END, outcome, unit_net,
    jsonb_build_object('score_rule',jsonb_build_object('provider','leyu','period',period,'market_id',market_id,
        'reference',CASE WHEN id IN (2,3) THEN 'leyu-football-fulltime-live-handicap-e448-20261106'
                         ELSE 'leyu-football-period-total-e330-20261106' END,
        'score_basis',CASE WHEN id IN (2,3) THEN 'remaining_period' ELSE 'full_period' END),
        'effective_signal',jsonb_build_object('score_basis',CASE WHEN id IN (2,3) THEN 'remaining_period' ELSE 'full_period' END)),
    created_at + interval '7 hours', 'verified-test' FROM v2_expert_signals;

ALTER TABLE v2_bet_orders ADD COLUMN provider_order_no text;
CREATE TABLE v2_order_receipt_evidence (
    id integer PRIMARY KEY, order_id integer, payload jsonb, observed_at timestamptz
);

INSERT INTO v2_expert_signals
SELECT n, fixture_id, phase, market_kind, '18', 'first_half', line, selection,
       score_basis, score_baseline, created_at, recommendation_price, outcome, unit_net, result_version
FROM v2_expert_signals CROSS JOIN generate_series(14,19) n WHERE id=1;

INSERT INTO v2_bet_orders
SELECT id, id, id, created_at + interval '8 hours', created_at + interval '8 hours',
       created_at + interval '10 seconds', 5, 2.10, 5.50, 'settled', false, id::text
FROM v2_expert_signals WHERE id>=14;

ALTER TABLE v2_expert_signals ADD COLUMN effective_stake numeric;
ALTER TABLE v2_signal_settlements ADD COLUMN effective_stake numeric;
ALTER TABLE v2_signal_settlements ADD COLUMN reason text;

INSERT INTO v2_expert_signals
SELECT n, fixture_id, phase, 'handicap', CASE WHEN n=23 THEN '19' ELSE '4' END,
       CASE WHEN n=23 THEN 'first_half' ELSE 'full_time' END, -1.25, 'home', 'unknown',
       '{"home":0,"away":0,"half_home":0,"half_away":0}', created_at, recommendation_price,
       'loss', -1, result_version, 1
FROM v2_expert_signals CROSS JOIN generate_series(20,23) n WHERE id=1;
INSERT INTO v2_bet_orders
SELECT id, id, id, created_at + interval '8 hours', created_at + interval '8 hours',
       created_at + interval '10 seconds', 5, 2.10, -5, 'settled', false, id::text
FROM v2_expert_signals WHERE id BETWEEN 20 AND 23;
INSERT INTO v2_quote_evidence
SELECT CASE WHEN stage='preflight_validated' THEN id ELSE id+100 END, id,
       fixture_id, market_id, period, selection, line, 2.10,
       jsonb_build_object('stage',stage,'attempt_id',id::text,'signal_id',id::text,
           'phase',phase,'market_kind',market_kind,'score_basis',score_basis,
           'prepared_digest','handicap-digest','preflight_evidence_id',id::text,'score_baseline',score_baseline,
           'score_benchmark',CASE WHEN id=21 AND stage='submission_authorized' THEN '1:0' ELSE '0:0' END),
       'order_attempt', created_at + interval '5 seconds'
FROM v2_expert_signals CROSS JOIN (VALUES ('preflight_validated'),('submission_authorized')) stages(stage)
WHERE id BETWEEN 20 AND 23;
INSERT INTO v2_order_receipt_evidence
SELECT id, id, jsonb_build_object('state','settled','order_no',provider_order_no,'net_profit','-5'), receipt_observed_at
FROM v2_bet_orders WHERE id BETWEEN 20 AND 23;
INSERT INTO v2_signal_settlements(signal_id,fixture_id,result_version,outcome,unit_net,calculation,created_at,
    algorithm_version,reason,effective_stake)
SELECT id,fixture_id,result_version,outcome,unit_net,
       jsonb_build_object('signal',jsonb_build_object('sport','football','kind',market_kind,'period',period,
           'phase',phase,'selection',selection,'score_basis',score_basis,'score_baseline',score_baseline,
           'line',line::text,'price',recommendation_price::text,'line_basis','provider_home'),
           'result',jsonb_build_object('status','confirmed')),
       created_at + interval '7 hours','verified-test','asian_split:zero_baseline_score_equivalent:provider_home_line',
       CASE WHEN id=22 THEN 0.5 ELSE effective_stake END
FROM v2_expert_signals WHERE id BETWEEN 20 AND 23;

INSERT INTO v2_quote_evidence
SELECT id, id, fixture_id, market_id, period, selection, line, 2.10,
       jsonb_build_object('stage','preflight_validated','attempt_id',id::text,'signal_id',id::text,
           'phase',phase,'market_kind',market_kind,'score_basis',score_basis,'prepared_digest','digest'),
       'order_attempt', created_at + interval '5 seconds'
FROM v2_expert_signals WHERE id BETWEEN 14 AND 19;

ALTER TABLE v2_expert_signals ADD COLUMN settlement_reason text;
UPDATE v2_expert_signals SET settlement_reason='result_not_confirmed' WHERE id=15;
UPDATE v2_expert_signals SET settlement_reason='asian_line:score_basis_outcome_equivalent' WHERE id=16;

INSERT INTO v2_quote_evidence
SELECT id+100, id, fixture_id, market_id, period, selection, line, 2.10,
       jsonb_build_object('stage','submission_authorized','attempt_id',id::text,'signal_id',id::text,
           'preflight_evidence_id',id::text,'prepared_digest',CASE WHEN id=15 THEN 'different' ELSE 'digest' END),
       'order_attempt', created_at + interval '6 seconds'
FROM v2_expert_signals WHERE id BETWEEN 14 AND 19;

INSERT INTO v2_order_receipt_evidence
SELECT id, id, jsonb_build_object('state','settled','order_no',
       CASE WHEN id=19 THEN 'different' ELSE provider_order_no END,
       'net_profit',CASE WHEN id=16 THEN '5.49' ELSE '5.50' END), receipt_observed_at
FROM v2_bet_orders WHERE id BETWEEN 14 AND 19;

INSERT INTO v2_signal_settlements(signal_id,fixture_id,result_version,outcome,unit_net,calculation,created_at,
    algorithm_version,reason)
SELECT id, fixture_id, result_version, outcome, unit_net,
       jsonb_build_object('signal',jsonb_build_object('sport','football','kind',market_kind,'period',period,
           'phase',phase,'selection',selection,'score_basis',score_basis,'score_baseline',
           CASE WHEN id=17 THEN '{"home":2,"away":0}'::jsonb ELSE score_baseline END,
           'line',CASE WHEN id=18 THEN 'invalid' ELSE line::text END,'price',recommendation_price::text),
           'result',jsonb_build_object('status','confirmed')),
       created_at + interval '7 hours', 'verified-test', 'asian_line:score_basis_outcome_equivalent'
FROM v2_expert_signals WHERE id BETWEEN 14 AND 19;
