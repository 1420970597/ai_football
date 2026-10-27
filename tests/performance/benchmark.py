"""HTTP through production ASGI plus separate SQL/EXPLAIN isolation baseline."""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

import asyncpg
import httpx

from app.expert_statistics import QUALITY_METRIC_SQL, StatisticsScope, signal_scope
from app.result_ledger import result_facts
from app.settlement import settle
from seed import DATABASE, EXPERT, HOST

REPORTS = Path('/w/reports/performance')
API = 'http://ai-football-v2-perf-api:8080'


def source_digests():
    return {str(path.relative_to('/w')): hashlib.sha256(path.read_bytes()).hexdigest()
            for root in (Path('/w/services/api/app'), Path('/w/services/api/migrations'), Path('/w/tests/performance'))
            for path in sorted(root.rglob('*')) if path.is_file() and path.suffix in {'.py', '.sql', '.sh'}}

HOME_SQL = ("SELECT id,source_id,sport,competition_id,competition_name,home_team,away_team,kickoff_at,phase,"
            "match_state,home_score,away_score,clock_minute,source_updated_at,received_at FROM v2_fixtures "
            "WHERE phase IN ('live','scheduled') ORDER BY kickoff_at,id LIMIT 51")
QUOTE_SQL = ("WITH ranked AS (SELECT id AS quote_id,fixture_id,market_id,market_name,period,market_kind,line,selection,price,status,is_main,source_updated_at,received_at,"
             "row_number() OVER(PARTITION BY fixture_id ORDER BY is_main DESC,source_updated_at DESC NULLS LAST,market_id,line,selection) AS position "
             "FROM v2_current_quotes WHERE fixture_id=ANY($1::text[])) SELECT quote_id,fixture_id,market_id,market_name,period,market_kind,line,selection,price,status,is_main,source_updated_at,received_at "
             "FROM ranked WHERE position<=24 ORDER BY fixture_id,market_id,line,selection")
AUTHOR_SQL = ("SELECT m.id,m.raw_text,m.media_objects,m.media_state,m.posted_at,m.edited_at,m.revision,m.deleted_at,e.display_nickname,"
              "j.state AS analysis_state,j.last_error AS analysis_error FROM v2_expert_messages m JOIN v2_experts e ON e.id=m.expert_id "
              "LEFT JOIN LATERAL(SELECT state,last_error FROM v2_analysis_jobs WHERE message_id=m.id ORDER BY created_at DESC LIMIT 1) j ON TRUE "
              "WHERE m.expert_id=$1 ORDER BY m.posted_at DESC,m.id DESC LIMIT 31")
TOOL_SQL = ("SELECT f.id,row_number() OVER(ORDER BY f.kickoff_at,f.id),to_jsonb(f) FROM v2_fixtures f "
            "WHERE f.phase IN ('live','scheduled') AND ($1='all' OR f.phase=$1) "
            "AND (lower(f.home_team) LIKE $2 OR lower(f.away_team) LIKE $2 OR $3=ANY(f.home_aliases) OR $3=ANY(f.away_aliases)) "
            "AND ($4='' OR lower(f.competition_name) LIKE '%'||$4||'%') "
            "AND ($5::timestamptz IS NULL OR f.kickoff_at >= $5) AND ($6::timestamptz IS NULL OR f.kickoff_at <= $6)")


def distribution(values):
    if not values or any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError('measurement samples must be nonempty finite nonnegative milliseconds')
    ordered = sorted(values)
    def percentile(fraction):
        return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 3)
    return {'samples': len(values), 'p50_ms': percentile(.50), 'p95_ms': percentile(.95),
            'min_ms': round(ordered[0], 3), 'max_ms': round(ordered[-1], 3), 'quantile_method': 'nearest_rank'}


def statistics_cases(now):
    scope = StatisticsScope(now-timedelta(days=30), now+timedelta(days=1))
    prefix, args = signal_scope(scope)
    aggregate = (prefix + 'SELECT e.id,e.display_nickname,e.avatar_object_key,e.enabled,' + QUALITY_METRIC_SQL +
        ' FROM v2_experts e LEFT JOIN scoped s ON s.expert_id=e.id '
        'WHERE e.enabled AND ($6::uuid IS NULL OR e.id=$6) GROUP BY e.id '
        'ORDER BY unit_net DESC,total_signals DESC,e.display_nickname,e.id')
    source_prefix, source_args = signal_scope(scope,classify=False)
    sources = (source_prefix + 'SELECT DISTINCT x.id,x.display_name FROM v2_expert_sources x '
        'JOIN v2_expert_messages m ON m.source_id=x.id JOIN scoped s ON s.message_id=m.id '
        'JOIN v2_experts e ON e.id=s.expert_id WHERE x.enabled AND e.enabled ORDER BY x.display_name,x.id')
    return {'statistics_aggregation':(aggregate,tuple(args)), 'statistics_source_options':(sources,tuple(source_args))}


async def validate_statistics_fixture(conn):
    missing = await conn.fetchval("SELECT count(*) FROM v2_expert_signals s WHERE NOT EXISTS(SELECT 1 FROM v2_quote_evidence e WHERE e.reference_id=s.id AND e.evidence_kind='expert_signal')")
    if missing:
        raise RuntimeError('benchmark signals lack immutable quote evidence')
    rows = await conn.fetch("SELECT l.*,r.status,r.home_score,r.away_score,r.half_home_score,r.half_away_score,r.provider_reference,r.source_updated_at,r.evidence,r.content_hash,f.sport "
        "FROM v2_signal_settlements l JOIN v2_fixture_results r ON r.fixture_id=l.fixture_id AND r.version=l.result_version JOIN v2_fixtures f ON f.id=l.fixture_id")
    for row in rows:
        facts = result_facts(row)
        digest = hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        value = settle(row['calculation']['signal'], facts)
        if row['content_hash'] != digest or row['calculation']['result'] != facts or (value.outcome,value.unit_net,value.effective_stake,value.reason) != tuple(row[k] for k in ('outcome','unit_net','effective_stake','reason')):
            raise RuntimeError('benchmark settlement fixture is not reproducible')
    return {'quote_snapshots_complete':True,'reproduced_settlements':len(rows),'synthetic_official_results_only':True}


async def measure_sql(conn, sql, args=(), iterations=25):
    for _ in range(3):
        await conn.fetch(sql, *args)
    elapsed, rows = [], []
    for _ in range(iterations):
        started = time.perf_counter()
        rows = await conn.fetch(sql, *args)
        elapsed.append((time.perf_counter() - started) * 1000)
    plan = await conn.fetchval('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) ' + sql, *args)
    return {**distribution(elapsed), 'returned_rows': len(rows), 'query': sql, 'explain': plan}


async def measure_http(client, method, path, *, headers=None, body=None, iterations=30, warmup=3):
    elapsed, sizes, last = [], [], None
    for index in range(iterations + warmup):
        payload = body(index) if callable(body) else body
        started = time.perf_counter()
        response = await client.request(method, API + path, headers=headers, json=payload)
        if response.status_code != 200:
            raise RuntimeError(f'benchmark HTTP status {response.status_code}: {path}')
        last = response.json()
        if index >= warmup:
            elapsed.append((time.perf_counter() - started) * 1000)
            sizes.append(len(response.content))
    return {**distribution(elapsed), 'warmup_excluded': warmup, 'concurrency': 1,
            'response_bytes_min': min(sizes), 'response_bytes_max': max(sizes)}, last


def tool_body(index):
    number = index + 1
    return {'review_id': str(uuid.UUID(hashlib.md5(f'synthetic-job-{number}'.encode()).hexdigest())),
            'attempt_id': str(uuid.UUID(hashlib.md5(f'synthetic-attempt-{number}'.encode()).hexdigest())),
            'team_query': 'Perf Club 00', 'page_size': 25}


async def exercise_pagination(client):
    context = tool_body(98)
    body, total, pages = dict(context), 0, 0
    headers = {'Authorization': 'Bearer synthetic-performance-tools'}
    started = time.perf_counter()
    while True:
        response = await client.post(API + '/internal/mcp/search_matches', json=body, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f'tool pagination failed HTTP{response.status_code}: {response.text[:300]}')
        value = response.json()
        pages += 1
        total += len(value['items'])
        if not value['next_cursor']:
            break
        body['cursor'] = value['next_cursor']
    if total != value['total_candidates'] or total <= 25 or pages < 2:
        raise RuntimeError('tool did not fully enumerate candidates')
    resolved = await client.post(API + '/internal/mcp/resolve_match', headers=headers,
        json={key: context[key] for key in ('review_id', 'attempt_id')} |
             {'search_key': value['search_key'], 'team_a': 'Perf Club 00001 Home', 'team_b': 'Perf Club 00001 Away'})
    if resolved.status_code != 200 or not resolved.json().get('resolved'):
        raise RuntimeError('tool exact resolution failed')
    walk_elapsed = round((time.perf_counter() - started) * 1000, 3)
    market_body = {key: context[key] for key in ('review_id', 'attempt_id')} | {'fixture_id': 'perf-match-00001'}
    markets, payload = await measure_http(client, 'POST', '/internal/mcp/list_match_markets', headers=headers,
                                          body=market_body, iterations=20)
    return {'candidate_count': total, 'pages': pages, 'page_size': 25,
            'walk_and_resolve_ms': walk_elapsed,
            'market_count': len(payload['markets']), 'markets_http': markets}


async def statistics_main():
    """Measure only the current statistics route; no MCP writes contaminate it."""
    if os.environ.get('PERFORMANCE_SYNTHETIC_ONLY') != 'true':
        raise RuntimeError('synthetic-only flag required')
    source_before=source_digests()
    started=time.perf_counter()
    async with httpx.AsyncClient(timeout=15,trust_env=False) as client:
        for attempt in range(30):
            try:
                response=await client.get(API+'/readyz')
                if response.status_code==200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(.5)
        else:
            raise RuntimeError('isolated production API not ready')
        conn=await asyncpg.connect(f'postgresql://performance_test@{HOST}/{DATABASE}',command_timeout=30)
        try:
            for name in ('json','jsonb'):
                await conn.set_type_codec(name,schema='pg_catalog',encoder=json.dumps,decoder=json.loads)
            if await conn.fetchval('SELECT current_user')!='performance_test' or await conn.fetchval('SELECT current_database()')!=DATABASE:
                raise RuntimeError('unexpected performance target')
            validation=await validate_statistics_fixture(conn)
            queries=statistics_cases(datetime.now(timezone.utc))
            timings={key:await measure_sql(conn,sql,args) for key,(sql,args) in queries.items()}
            baseline_sql,baseline_args=queries['statistics_aggregation']
            trial_sql=(baseline_sql.replace('WITH checked AS MATERIALIZED (','WITH checked AS (',1)
                       if 'WITH checked AS MATERIALIZED (' in baseline_sql else
                       baseline_sql.replace('WITH checked AS (','WITH checked AS MATERIALIZED (',1))
            if trial_sql==baseline_sql:
                raise RuntimeError('temporary statistics experiment did not alter the expected CTE')
            baseline_rows=[dict(row) for row in await conn.fetch(baseline_sql,*baseline_args)]
            trial_rows=[dict(row) for row in await conn.fetch(trial_sql,*baseline_args)]
            if baseline_rows!=trial_rows:
                raise RuntimeError('temporary statistics materialization changed measured results')
            trial_timing=await measure_sql(conn,trial_sql,baseline_args)
            http,body=await measure_http(client,'GET','/api/v1/public/experts?days=30')
            expected={'total_signals':10000,'eligible_signals':10000,'excluded_signals':0,'pending':2500,'win':2500,'loss':2500,'void':2500,'settled_signals':7500}
            if len(body['items'])!=100 or any(sum(row[key] for row in body['items'])!=count for key,count in expected.items()) or len(body['source_options'])!=1:
                raise RuntimeError('current statistics HTTP does not match evidence-backed fixtures')
            if source_before!=source_digests():
                raise RuntimeError('frozen source changed during measurement')
            report={'format':'isolated-evidence-statistics-v1','synthetic_only':True,'production_performance_verified':False,
                'created_at':datetime.now(timezone.utc).isoformat(),'source_commit':os.environ.get('PERFORMANCE_SOURCE_COMMIT'),
                'source_sha256':source_before,'source_frozen':True,'verifier_image_id':os.environ.get('PERFORMANCE_IMAGE_ID'),
                'postgres_image_id':os.environ.get('PERFORMANCE_PG_IMAGE_ID'),'postgres_version':await conn.fetchval('SHOW server_version'),
                'seed':json.loads((REPORTS/'seed.json').read_text()),'fixture_validation':validation,'statistics_expected':expected,
                'sql':timings,'http':{'statistics':http},
                'temporary_statistics_experiment':{'change':'toggle checked CTE materialization against production query','query':trial_sql,
                    'timing':trial_timing,'same_ordered_result':True,'production_source_modified':False},
                'resource_caps':{'postgres_cpus':.35,'api_cpus':.25,'client_cpus':.15,
                'postgres_memory_mib':768,'api_memory_mib':256,'client_memory_mib':256,'data_tmpfs_mib':512,
                'postgres_max_wal_mib':128,'postgres_checkpoint_seconds':30},
                'elapsed_seconds':round(time.perf_counter()-started,3),
                'limitations':['synthetic_data_only','warm_sequential_keepalive_not_capacity_or_production_p95',
                    'local_docker_tmpfs_not_remote_vps_or_disk_io','no_browser_or_hermes_or_upstream_calls',
                    'statistics_only_not_complete_performance_suite']}
            (REPORTS/'statistics-baseline.json').write_text(json.dumps(report,sort_keys=True,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({'sql':{key:{field:value[field] for field in ('samples','p50_ms','p95_ms')} for key,value in timings.items()},
                              'temporary_alternate_plan':{key:trial_timing[key] for key in ('samples','p50_ms','p95_ms')},
                              'http':http,'fixture_validation':validation,'elapsed_seconds':report['elapsed_seconds']},sort_keys=True))
        finally:
            await conn.close()


async def pagination_probe():
    if os.environ.get('PERFORMANCE_SYNTHETIC_ONLY')!='true':
        raise RuntimeError('synthetic-only flag required')
    if '--pagination-asgi-probe' in sys.argv:
        from app.main import app
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),timeout=15) as client:
                print(json.dumps(await exercise_pagination(client),sort_keys=True))
    else:
        async with httpx.AsyncClient(timeout=15,trust_env=False) as client:
            print(json.dumps(await exercise_pagination(client),sort_keys=True))


async def main():
    if os.environ.get('PERFORMANCE_SYNTHETIC_ONLY') != 'true':
        raise RuntimeError('synthetic-only flag required')
    started = time.perf_counter()
    source_before = source_digests()
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        for attempt in range(30):
            try:
                ready = await client.get(API + '/readyz')
                if ready.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(.5)
        else:
            raise RuntimeError('isolated production API not ready')
        conn = await asyncpg.connect(f'postgresql://performance_test@{HOST}/{DATABASE}', command_timeout=30)
        for name in ('json', 'jsonb'):
            await conn.set_type_codec(name, schema='pg_catalog', encoder=json.dumps, decoder=json.loads)
        try:
            if await conn.fetchval('SELECT current_user') != 'performance_test' or await conn.fetchval('SELECT current_database()') != DATABASE:
                raise RuntimeError('unexpected performance target')
            home_rows = await conn.fetch(HOME_SQL)
            fixture_ids = [row['id'] for row in home_rows[:50]]
            now = datetime.now(timezone.utc)
            fixture_validation = await validate_statistics_fixture(conn)
            sql_cases = {
                'homepage_fixture_page': (HOME_SQL, ()),
                'homepage_quote_summary': (QUOTE_SQL, (fixture_ids,)),
                'detail_fixture': ('SELECT id,home_team,away_team,phase,kickoff_at FROM v2_fixtures WHERE id=$1', ('perf-match-00001',)),
                'detail_quotes': ('SELECT id,market_id,market_name,period,market_kind,line,selection,price,status,is_main,score_basis,source_updated_at,received_at FROM v2_current_quotes WHERE fixture_id=$1 ORDER BY market_id,line,selection LIMIT 500', ('perf-match-00001',)),
                'detail_signals': ('SELECT s.id,s.expert_id,e.display_nickname,s.market_id,s.line,s.selection,s.recommendation_price,s.outcome,s.unit_net FROM v2_expert_signals s JOIN v2_experts e ON e.id=s.expert_id WHERE s.fixture_id=$1 ORDER BY s.created_at DESC LIMIT 100', ('perf-match-00001',)),
                'author_message_page': (AUTHOR_SQL, (uuid.UUID(EXPERT),)),
                'review_candidate_selection': (TOOL_SQL, ('all', '%perf club 000%', 'perf club 000', '', None, None)),
                **statistics_cases(now),
            }
            pure_sql = {name: await measure_sql(conn, query, args) for name, (query, args) in sql_cases.items()}
            http = {}
            http['homepage'], homepage = await measure_http(client, 'GET', '/api/v1/public/matches?page_size=50')
            if len(homepage['items']) != 50 or not homepage['next_cursor']:
                raise RuntimeError('homepage was not populated beyond first screen')
            http['detail'], detail = await measure_http(client, 'GET', '/api/v1/public/matches/perf-match-00001')
            if len(detail['markets']) != 5:
                raise RuntimeError('detail quotes missing')
            history_headers = {'Authorization': 'Bearer synthetic-performance-user'}
            http['author_messages'], messages = await measure_http(client, 'GET', f'/api/v1/public/experts/{EXPERT}/messages?limit=30', headers=history_headers)
            if len(messages['items']) != 30 or not messages['next_cursor']:
                raise RuntimeError('author history did not exceed first page')
            http['statistics'], statistics = await measure_http(client, 'GET', '/api/v1/public/experts?days=30')
            if len(statistics['items']) != 100:
                raise RuntimeError('statistics expert population mismatch')
            expected = {'total_signals':10000,'eligible_signals':10000,'excluded_signals':0,'pending':2500,'win':2500,'loss':2500,'void':2500,'settled_signals':7500}
            if any(sum(row[key] for row in statistics['items']) != count for key,count in expected.items()) or len(statistics['source_options']) != 1:
                raise RuntimeError('current statistics HTTP did not verify complete evidence-backed population')
            (REPORTS/'statistics-checkpoint.json').write_text(json.dumps({'format':'evidence-statistics-checkpoint-v1',
                'synthetic_only':True,'fixture_validation':fixture_validation,'statistics_expected':expected,
                'source_sha256':source_before,'seed':json.loads((REPORTS/'seed.json').read_text()),
                'sql':{key:value for key,value in pure_sql.items() if key.startswith('statistics_')},
                'http':{'statistics':http['statistics']}},sort_keys=True,ensure_ascii=False,indent=2)+'\n')
            http['review_search_first_page'], _ = await measure_http(client, 'POST', '/internal/mcp/search_matches',
                headers={'Authorization': 'Bearer synthetic-performance-tools'}, body=tool_body, iterations=20)
            pagination = await exercise_pagination(client)
            http['review_market_read'] = pagination.pop('markets_http')
            baseline_ids = [item['id'] for item in homepage['items']]
            trial_ddl = "CREATE INDEX perf_trial_active_kickoff_idx ON v2_fixtures(kickoff_at,id) WHERE phase IN ('live','scheduled')"
            await conn.execute(trial_ddl)
            await conn.execute('ANALYZE v2_fixtures')
            trial_sql = await measure_sql(conn, HOME_SQL)
            trial_http, trial_home = await measure_http(client, 'GET', '/api/v1/public/matches?page_size=50')
            if baseline_ids != [item['id'] for item in trial_home['items']]:
                raise RuntimeError('experimental index changed ordered fixture results')
            sizes = {row['table_name']: row['bytes'] for row in await conn.fetch("SELECT relname AS table_name,pg_total_relation_size(relid) AS bytes FROM pg_catalog.pg_statio_user_tables ORDER BY relname")}
            if source_before != source_digests():
                raise RuntimeError('project source changed while performance measurement was running')
            report = {'format': 'isolated-performance-v2-evidence-statistics', 'synthetic_only': True, 'production_performance_verified': False,
                'created_at': datetime.now(timezone.utc).isoformat(), 'source_commit': os.environ.get('PERFORMANCE_SOURCE_COMMIT'),
                'source_worktree': 'includes_uncommitted_project_changes', 'verifier_image_id': os.environ.get('PERFORMANCE_IMAGE_ID'),
                'postgres_image_id': os.environ.get('PERFORMANCE_PG_IMAGE_ID'), 'source_sha256': source_before,
                'seed': json.loads((REPORTS/'seed.json').read_text()), 'fixture_validation':fixture_validation, 'statistics_expected':expected, 'postgres_version': await conn.fetchval('SHOW server_version'),
                'resource_caps': {'postgres': {'cpus': .35, 'memory_mib': 768, 'data_tmpfs_mib': 512, 'shared_buffers_mib': 32,'max_wal_mib':128,'checkpoint_seconds':30},
                                  'api': {'cpus': .25, 'memory_mib': 256, 'uvicorn_workers': 1},
                                  'client': {'cpus': .15, 'memory_mib': 256}, 'active_cpu_total': .75},
                'http': http, 'sql': pure_sql, 'pagination': pagination,
                'experimental_index': {'ddl': trial_ddl, 'sql': trial_sql, 'http': trial_http, 'same_ordered_result': True,
                                       'applied_to_source_migrations': False, 'scope': 'temporary_database_only'},
                'relation_bytes': sizes, 'elapsed_seconds': round(time.perf_counter()-started,3),
                'limitations': ['local_private_docker_network_and_tmpfs_not_remote_vps_or_disk_io',
                    'warm_keepalive_single_concurrency_not_capacity_or_production_p95', 'no_browser_rendering_or_hermes_or_live_provider_calls',
                    'quotes_are_synthetic_and_test_freshness_limit_is_3600_seconds',
                    'sql_client_roundtrip_plus_decode_is_separate_from_explain_server_execution_time',
                    'raw_query_timing_is_not_the_whole_multiquery_http_route']}
            (REPORTS/'baseline.json').write_text(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2)+'\n')
            print(json.dumps({'synthetic_only': True, 'http': {key: {field: value[field] for field in ('samples','p50_ms','p95_ms')} for key,value in http.items()},
                              'candidate_walk': pagination, 'trial_home_sql_p95_ms': trial_sql['p95_ms'],
                              'trial_home_http_p95_ms': trial_http['p95_ms'], 'elapsed_seconds': report['elapsed_seconds']}, sort_keys=True))
        finally:
            await conn.close()


if __name__ == '__main__':
    asyncio.run(statistics_main() if '--statistics-only' in sys.argv else pagination_probe() if '--pagination-probe' in sys.argv or '--pagination-asgi-probe' in sys.argv else main())
