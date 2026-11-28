CREATE TABLE v2_fixtures (id text PRIMARY KEY, sport text);
CREATE TABLE v2_expert_signals (
    id int PRIMARY KEY, phase text, market_kind text, market_id text, period text,
    fixture_id text, line numeric, selection text, score_basis text, score_baseline jsonb,
    recommendation_price numeric, quote_time timestamptz, created_at timestamptz, withdrawn_at timestamptz
);
CREATE TABLE v2_quote_evidence (
    id int PRIMARY KEY, reference_id int, evidence_kind text, fixture_id text, market_id text, period text,
    selection text, line numeric, price numeric, source_updated_at timestamptz, score_baseline jsonb,
    captured_at timestamptz, evidence jsonb
);
CREATE TABLE v2_review_market_reads (
    job_id uuid, attempt_id uuid, quote_id uuid, created_at timestamptz, source_updated_at timestamptz,
    price numeric, quote_context jsonb, PRIMARY KEY (job_id, attempt_id, quote_id)
);

INSERT INTO v2_fixtures VALUES ('football-1','football');
INSERT INTO v2_expert_signals
SELECT n,'live','handicap','2','full_time','football-1',
       CASE WHEN n=8 THEN -0.5 ELSE -0.75 END,'home','remaining_period',
       '{"home":1,"away":0,"half_home":0,"half_away":0}',1.91,
       now()-interval '10 minutes',now()-interval '9 minutes',NULL
FROM generate_series(1,8) n;

INSERT INTO v2_quote_evidence
SELECT s.id,s.id,'expert_signal',fixture_id,market_id,period,selection,line,recommendation_price,
       quote_time,score_baseline,created_at,jsonb_build_object(
         'job_id',md5(s.id::text)::uuid,'attempt_id',md5('attempt')::uuid,
         'quote_id',md5(s.id::text || '-selected')::uuid,'market_kind',s.market_kind,'score_basis',s.score_basis)
FROM v2_expert_signals s WHERE s.id<>7;

INSERT INTO v2_review_market_reads
SELECT md5(s.id::text)::uuid,md5('attempt')::uuid,md5(s.id::text||'-'||suffix)::uuid,
       s.created_at-interval '5 seconds',
       s.quote_time-CASE WHEN s.id=2 AND suffix='tight-away' THEN interval '1 second' ELSE interval '0' END,
       1.91,jsonb_build_object('fixture_id',s.fixture_id,'market_id',s.market_id,'period',s.period,'phase',s.phase,
            'market_kind',s.market_kind,'score_basis',s.score_basis,'play_id','2','selection',side,
            'selection_id',suffix,'market_instance_id',instance,'line',coalesce(q.line,s.line)::text,
            'home_score',CASE WHEN s.id=4 AND instance='tight' THEN 2 ELSE 1 END,
            'away_score',0,'half_home_score',0,'half_away_score',0)
FROM v2_expert_signals s CROSS JOIN (VALUES
    ('selected','home','target',NULL::numeric), ('target-away','away','target',NULL),
    ('loose-home','home','loose',-0.5), ('loose-away','away','loose',-0.5),
    ('tight-home','home','tight',-1.5), ('tight-away','away','tight',-1.5),
    ('extra-home','home','extra',-1.5), ('extra-away','away','extra',-1.5)
) AS q(suffix,side,instance,line)
WHERE (instance<>'extra' OR s.id=3)
  AND (s.id<>5 OR suffix<>'tight-away')
  AND (s.id<>6 OR instance<>'tight')
  AND (s.id<>8 OR instance='target');
