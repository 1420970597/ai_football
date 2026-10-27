"""真实框架+SDK+数据库，提供方为显式确定性fixture，无外部模型或上游访问。"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import uuid
from urllib.parse import urlsplit

import asyncpg
import httpx

from app.hermes_client import SYSTEM_PROMPT


async def run():
    dsn = os.environ['INTEGRATION_DATABASE_URL']
    parts = urlsplit(dsn)
    if parts.hostname != 'ai-football-v2-pgverify' or parts.path != '/v2_integration_test':
        raise RuntimeError('only task-owned integration database is allowed')
    conn = await asyncpg.connect(dsn)
    for kind in ('json','jsonb'):
        await conn.set_type_codec(kind, schema='pg_catalog', encoder=json.dumps, decoder=json.loads)
    source, expert, message, job, attempt = [uuid.uuid4() for _ in range(5)]
    now = datetime.now(timezone.utc)
    prefix = 'integration-' + job.hex
    await conn.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,$2,'隔离群')", source, prefix)
    await conn.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,$2,'隔离专家')", expert, prefix)
    await conn.execute('INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)', source, expert)
    await conn.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,'1','Alpha104 对 Beta104，赛前大2.5',now())", message, source, expert)
    await conn.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,attempt_id,lease_until) VALUES($1,$2,'running',$3,$4,now()+interval '30 minutes')", job, message, str(job), attempt)
    for i in range(105):
        fixture = f'{prefix}-{i}'
        await conn.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase,source_updated_at) VALUES($1,$1,'football',$5,$2,$3,$4,'scheduled',now())", fixture, f'Alpha{i}', f'Beta{i}', now+timedelta(minutes=i), '隔离联赛-'+str(job))
    await conn.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,market_kind,line,selection,price,source_updated_at,score_basis) VALUES($1,$2,'total','全场大小','total',2.5,'over',1.95,now(),'full_period')", uuid.uuid4(), f'{prefix}-104')
    image = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7GQAAAAASUVORK5CYII='
    session = f'expert-review-{job}-{attempt}'
    body = {'model':'hermes-agent','stream':False,'messages':[
        {'role':'system','content':SYSTEM_PROMPT}, {'role':'user','content':[
            {'type':'text','text':f'review_id: {job}\nattempt_id: {attempt}\n隔离消息：Alpha104 对 Beta104，赛前全场大2.5。调用 expert-review 技能并完整分页核实。'},
            {'type':'image_url','image_url':{'url':'data:image/png;base64,'+image,'detail':'high'}}]}]}
    async with httpx.AsyncClient(timeout=180, trust_env=False) as http:
        response = await http.post('http://ai-football-v2-hermes-integration:8642/v1/chat/completions', json=body,
                                   headers={'Authorization':'Bearer isolated-framework-key','X-Hermes-Session-Id':session})
        print('Hermes HTTP', response.status_code)
        if response.status_code != 200:
            print('response', response.text[:1500])
        response.raise_for_status()
        evidence = (await http.get('http://provider-fixture:8999/evidence')).json()
        print(json.dumps({k:v for k,v in evidence.items() if k != 'tool_results'}, ensure_ascii=False))
        signals = await conn.fetch('SELECT fixture_id,market_kind,period,line,selection,recommendation_price FROM v2_expert_signals WHERE message_id=$1', message)
        print('accepted_signals', len(signals))
        if not signals:
            print('last_tool_results', evidence.get('tool_results', [])[-2:])
        assert len(signals) == 1, 'framework must record exactly one verified recommendation'
        assert signals[0]['fixture_id'] == f'{prefix}-104'
        assert evidence['native_image_seen'], 'native image must reach the fixture provider'
        assert evidence['models'] == ['cline-pass/deepseek-v4.1-flash']
        assert evidence['project_skill_seen'], 'dedicated project skill must actually be loaded'
        assert set(evidence['tools']) == {
            'skill_view', 'skills_list', 'mcp__football__search_matches',
            'mcp__football__resolve_match', 'mcp__football__list_match_markets',
            'mcp__football__record_expert_signals',
        }, 'only the six audited project tools may reach the provider'
        search = await conn.fetchrow('SELECT page_count,total_candidates,complete FROM v2_match_searches WHERE job_id=$1', job)
        assert search['complete'] and search['total_candidates'] >= 105 and search['page_count'] >= 6
        print('continuous_search', dict(search))
        cleanup = await http.delete('http://ai-football-v2-hermes-integration:8642/api/sessions/'+session, headers={'Authorization':'Bearer isolated-framework-key'})
        print('session_cleanup', cleanup.status_code)
        assert cleanup.status_code in {200,204,404}
    await conn.close()


if __name__ == '__main__':
    asyncio.run(run())
