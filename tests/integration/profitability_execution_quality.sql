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
    'unknown', '{"home":1,"away":0}', :'anchor'::timestamptz, 2.00, 'win', 1.00, 2
FROM generate_series(1,5) n;
INSERT INTO v2_bet_orders
SELECT id, id, id, created_at + interval '8 hours', created_at + interval '8 hours',
    created_at + interval '10 seconds', 5, 2.10, 5.50, 'settled', false FROM v2_expert_signals;
INSERT INTO v2_quote_evidence
SELECT id, id, fixture_id, market_id, period, selection, line, 2.10,
    jsonb_build_object('stage','preflight_validated','attempt_id',id::text,'phase',phase,'market_kind',market_kind,
       'score_basis',score_basis,'score_baseline',score_baseline,'score_benchmark',CASE WHEN id=3 THEN '2:0' ELSE '1:0' END),
    'order_attempt', created_at + interval '5 seconds' FROM v2_expert_signals;
INSERT INTO v2_signal_settlements
SELECT id, fixture_id, CASE WHEN id=4 THEN 1 ELSE result_version END, outcome, unit_net,
    jsonb_build_object('score_rule',jsonb_build_object('provider','leyu','period',period,'market_id',market_id,
        'reference',CASE WHEN id IN (2,3) THEN 'leyu-football-fulltime-live-handicap-e448-20261106'
                         ELSE 'leyu-football-period-total-e330-20261106' END,
        'score_basis',CASE WHEN id IN (2,3) THEN 'remaining_period' ELSE 'full_period' END),
        'effective_signal',jsonb_build_object('score_basis',CASE WHEN id IN (2,3) THEN 'remaining_period' ELSE 'full_period' END)),
    created_at + interval '7 hours', 'verified-test' FROM v2_expert_signals;
