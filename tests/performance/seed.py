"""Create a fresh, strictly isolated synthetic performance database."""

import asyncio
from datetime import datetime, timezone
import json
import os
import time

import asyncpg

from app.database import apply_migrations
from app.security import token_hash

HOST = 'ai-football-v2-perf-pg'
DATABASE = 'test_performance_baseline'
EXPERT = 'a1000000-0000-4000-8000-000000000000'
SOURCE = 'b1000000-0000-4000-8000-000000000000'
USER = 'c1000000-0000-4000-8000-000000000000'
PLAN = 'd1000000-0000-4000-8000-000000000000'


async def main():
    if os.environ.get('PERFORMANCE_SYNTHETIC_ONLY') != 'true':
        raise RuntimeError('explicit synthetic-only flag required')
    started = time.perf_counter()
    control = await asyncpg.connect(f'postgresql://performance_test@{HOST}/postgres', timeout=5)
    try:
        if await control.fetchval('SELECT 1 FROM pg_database WHERE datname=$1', DATABASE):
            raise RuntimeError('performance target already exists; refusing overwrite')
        await control.execute('CREATE DATABASE ' + DATABASE)
    finally:
        await control.close()
    conn = await asyncpg.connect(f'postgresql://performance_test@{HOST}/{DATABASE}', command_timeout=120)
    await conn.set_type_codec('jsonb', schema='pg_catalog', encoder=json.dumps, decoder=json.loads)
    try:
        await apply_migrations(conn)
        async with conn.transaction():
            await conn.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1::uuid,'synthetic-performance@test.invalid','合成性能用户','not-a-login-hash','user')", USER)
            await conn.execute("INSERT INTO v2_sessions(token_hash,csrf_hash,user_id,session_kind,expires_at) VALUES($1,$2,$3::uuid,'user',now()+interval '1 hour')", token_hash('synthetic-performance-user'), token_hash('synthetic-performance-csrf'), USER)
            await conn.execute("INSERT INTO v2_membership_plans(id,code,name,price,duration_days,entitlements,enabled) VALUES($1::uuid,'synthetic-performance','合成性能权益',1,1,$2,true)", PLAN, {'expert_full_history': True})
            await conn.execute("INSERT INTO v2_memberships(id,user_id,plan_id,status,starts_at,ends_at,grant_source) VALUES(md5('synthetic-membership')::uuid,$1::uuid,$2::uuid,'active',now()-interval '1 hour',now()+interval '1 day','synthetic')", USER, PLAN)
            await conn.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1::uuid,'synthetic-performance-source','合成性能来源')", SOURCE)
            await conn.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) SELECT CASE WHEN g=0 THEN $1::uuid ELSE md5('synthetic-expert-'||g)::uuid END,'synthetic-author-'||g,'合成专家'||g FROM generate_series(0,99) g", EXPERT)
            await conn.execute('INSERT INTO v2_source_experts(source_id,expert_id) SELECT $1::uuid,id FROM v2_experts', SOURCE)
            await conn.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_id,competition_name,home_team,away_team,kickoff_at,phase,home_score,away_score,source_updated_at,source_time_quality,home_aliases) "
                "SELECT 'perf-match-'||lpad(g::text,5,'0'),'perf-match-'||lpad(g::text,5,'0'),'football','perf-league-'||(g%50),'Perf League '||(g%50),"
                "'Perf Club '||lpad(g::text,5,'0')||' Home','Perf Club '||lpad(g::text,5,'0')||' Away',now()+((g%240)-120)*interval '1 minute',"
                "CASE WHEN g%10=0 THEN 'finished' WHEN g%3=0 THEN 'live' ELSE 'scheduled' END,CASE WHEN g%3=0 THEN 1 END,CASE WHEN g%3=0 THEN 0 END,now(),'provided',ARRAY['perf-alias-'||g] FROM generate_series(1,10000) g")
            await conn.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,period,market_kind,line,selection,price,status,is_main,source_updated_at,source_time_quality,play_id,market_instance_id,selection_id) "
                "SELECT md5('synthetic-quote-'||g||'-'||q)::uuid,'perf-match-'||lpad(g::text,5,'0'),'perf-market-'||q,'合成盘口'||q,'full_time',"
                "CASE WHEN q<=2 THEN 'winner' WHEN q<=4 THEN 'total' ELSE 'handicap' END,CASE WHEN q<=2 THEN 0 WHEN q<=4 THEN 2.5 ELSE -.25 END,"
                "CASE q WHEN 1 THEN 'home' WHEN 2 THEN 'away' WHEN 3 THEN 'over' WHEN 4 THEN 'under' ELSE 'home' END,1.95+q*.01,'active',q<=2,now(),'provided',q::text,'synthetic-instance-'||g,q::text "
                "FROM generate_series(1,10000) g CROSS JOIN generate_series(1,5) q")
            await conn.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) SELECT md5('synthetic-message-'||g)::uuid,$1::uuid,"
                "CASE WHEN g%100=0 THEN $2::uuid ELSE md5('synthetic-expert-'||(g%100))::uuid END,g::text,'SYNTHETIC PERFORMANCE ONLY：合成原文，不是真实推荐 '||g,now()-(g%1440)*interval '1 minute' FROM generate_series(1,10000) g", SOURCE, EXPERT)
            await conn.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,attempt_id,lease_until,attempts) "
                "SELECT md5('synthetic-job-'||g)::uuid,md5('synthetic-message-'||g)::uuid,CASE WHEN g<=100 THEN 'running' ELSE 'unresolved' END,'synthetic-job-'||g,"
                "CASE WHEN g<=100 THEN md5('synthetic-attempt-'||g)::uuid END,CASE WHEN g<=100 THEN now()+interval '1 hour' END,CASE WHEN g<=100 THEN 1 ELSE 0 END FROM generate_series(1,10000) g")
            await conn.execute("INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,period,market_kind,line,selection,recommendation_price,quote_time,confidence,outcome,unit_net,effective_stake,settled_at) "
                "SELECT md5('synthetic-signal-'||g)::uuid,m.id,m.expert_id,'perf-match-'||lpad(g::text,5,'0'),'scheduled','perf-market-1','合成独赢','full_time','winner',0,'home',1.96,now(),.9,"
                "CASE g%4 WHEN 0 THEN 'win' WHEN 1 THEN 'loss' WHEN 2 THEN 'push' ELSE 'pending' END,CASE g%4 WHEN 0 THEN .96 WHEN 1 THEN -1 WHEN 2 THEN 0 END,"
                "CASE WHEN g%4=2 THEN 0 ELSE 1 END,CASE WHEN g%4<>3 THEN now() END FROM generate_series(1,10000) g JOIN v2_expert_messages m ON m.id=md5('synthetic-message-'||g)::uuid")
            await conn.execute("INSERT INTO v2_quote_evidence(id,fixture_id,market_id,market_name,period,line,selection,price,evidence_kind,reference_id,source_updated_at,evidence) "
                "SELECT md5('synthetic-evidence-'||g)::uuid,'perf-match-'||lpad(g::text,5,'0'),'perf-market-1','合成独赢','full_time',0,'home',1.96,'expert_signal',md5('synthetic-signal-'||g)::uuid,now(),'{\"origin\":\"synthetic-performance-only\"}'::jsonb FROM generate_series(1,10000) g")
        await conn.execute('ANALYZE')
        counts = {key: await conn.fetchval('SELECT count(*) FROM ' + table) for key, table in
                  {'fixtures': 'v2_fixtures', 'quotes': 'v2_current_quotes', 'messages': 'v2_expert_messages',
                   'signals': 'v2_expert_signals', 'experts': 'v2_experts', 'jobs': 'v2_analysis_jobs'}.items()}
        print(json.dumps({'synthetic_only': True, 'counts': counts, 'seed_seconds': round(time.perf_counter()-started,3),
                          'database': DATABASE, 'created_at': datetime.now(timezone.utc).isoformat()}, sort_keys=True))
    finally:
        await conn.close()


if __name__ == '__main__':
    asyncio.run(main())
