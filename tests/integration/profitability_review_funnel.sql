CREATE TABLE v2_analysis_jobs (
    id integer PRIMARY KEY, attempt_id integer, state text, review_mode text, last_error text, created_at timestamptz
);
CREATE TABLE v2_review_fixture_bindings (job_id integer, attempt_id integer);
CREATE TABLE v2_match_searches (job_id integer, attempt_id integer, complete boolean, total_candidates integer);
CREATE TABLE v2_analysis_traces (job_id integer, sequence_no integer, trace_kind text, payload jsonb);

INSERT INTO v2_analysis_jobs
SELECT id,2,CASE id WHEN 8 THEN 'failed' WHEN 9 THEN 'running' WHEN 10 THEN 'succeeded' ELSE 'unresolved' END,
       'real_time',CASE id WHEN 8 THEN 'source_edited' END,
       CASE id WHEN 12 THEN :'end'::timestamptz ELSE :'begin'::timestamptz END
FROM generate_series(1,12) id;

INSERT INTO v2_review_fixture_bindings VALUES (2,1),(3,2);
INSERT INTO v2_match_searches VALUES (2,1,true,0),(2,2,true,7);

INSERT INTO v2_analysis_traces
SELECT id,5,'system_review_result',jsonb_build_object('attempt_id','2','accepted_count',CASE id WHEN 10 THEN 1 ELSE 0 END,
    'completion_verified',id<>2,'completion_issues',CASE id WHEN 2 THEN '{"text_intent_unverified":true}'::jsonb ELSE '{}' END)
FROM generate_series(1,12) id WHERE id NOT IN (8,9,11);

INSERT INTO v2_analysis_traces VALUES
    (1,1,'mcp_list_match_markets','{"attempt_id":"1","status":"succeeded","market_coverage":{"matched_quotes":10}}'),
    (2,1,'mcp_search_matches','{"attempt_id":"2","status":"succeeded"}'),
    (2,2,'mcp_resolve_match','{"attempt_id":"2","status":"succeeded","result":{"resolved":false}}'),
    (4,1,'mcp_list_match_markets','{"attempt_id":"2","status":"failed"}'),
    (5,1,'mcp_list_match_markets','{"attempt_id":"2","status":"succeeded","market_coverage":{"matched_quotes":0}}'),
    (6,1,'mcp_list_match_markets','{"attempt_id":"2","status":"succeeded"}'),
    (7,1,'mcp_list_match_markets','{"attempt_id":"2","status":"succeeded","market_coverage":{"matched_quotes":1}}'),
    (12,1,'mcp_record_expert_signals','{"attempt_id":"2","status":"succeeded"}');
