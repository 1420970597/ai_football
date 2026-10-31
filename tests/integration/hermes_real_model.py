"""Real provider, task-owned PostgreSQL and synthetic labelled football inputs.

No production messages or quotes are written. The gateway is dedicated to this
test, and only its model provider settings are reused from protected config.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
import os
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

import asyncpg
import httpx
from PIL import Image, ImageDraw, ImageFont

from app.hermes_client import SYSTEM_PROMPT


def checked_dsn():
    value = os.environ['INTEGRATION_DATABASE_URL']
    parts = urlsplit(value)
    if parts.hostname != 'ai-football-v2-pgverify' or parts.path != '/v2_integration_test':
        raise RuntimeError('real model test requires the task-owned database')
    return value


def recommendation_image():
    image = Image.new('RGB', (1100, 360), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=30)
    for index, text in enumerate((
        'Expert football recommendation', 'Harbor FC vs Mountain United',
        'Full time: Total goals OVER 2.5', 'Pre-match. Use system price, not any advertised odds.',
    )):
        draw.text((30, 25 + index * 75), text, fill='black', font=font)
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


async def seed(conn, scenario):
    source, expert, message, job, attempt = [uuid.uuid4() for _ in range(5)]
    now = datetime.now(timezone.utc)
    prefix = 'real-model-isolated-' + job.hex
    raw = {
        'text': '足球推荐：Harbor FC 对 Mountain United，赛前全场总进球大2.5。',
        'image': '[消息仅包含图片，请从图片读取推荐]',
        'multiple': ('足球推荐：Harbor FC 对 Mountain United，赛前全场总进球大2.5；'
                     'River City 对 Forest Town，滚球全场总进球小3.5。'),
        'none': '大家早上好，今天出门吃饭。',
        'stale': '足球推荐：Harbor FC 对 Mountain United，赛前全场总进球大2.5。',
    }[scenario]
    async with conn.transaction():
        await conn.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,$2,'隔离模型群')", source, prefix)
        await conn.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,$2,'隔离模型专家')", expert, prefix)
        await conn.execute('INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)', source, expert)
        await conn.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,'1',$4,$5)", message, source, expert, raw, now)
        await conn.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,attempt_id,lease_until) VALUES($1,$2,'running',$3,$4,now()+interval '30 minutes')", job, message, str(job), attempt)
        for index, (home, away, phase, line, direction) in enumerate((
            ('Harbor FC', 'Mountain United', 'scheduled', '2.5', 'over'),
            ('River City', 'Forest Town', 'live', '3.5', 'under'),
        )):
            fixture = prefix + '-' + str(index)
            await conn.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase,source_updated_at,source_time_quality,home_score,away_score) VALUES($1,$1,'football',$2,$3,$4,$5,$6,now(),'upstream',$7,$8)",
                fixture, prefix, home, away, now + timedelta(hours=2) if phase == 'scheduled' else now-timedelta(minutes=20), phase,
                0 if phase == 'live' else None, 0 if phase == 'live' else None)
            await conn.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,market_kind,line,selection,price,source_updated_at,source_time_quality,score_basis) VALUES($1,$2,'total','全场大小','total',$3::numeric,$4,1.95,$5,'upstream','full_period')",
                uuid.uuid4(), fixture, line, direction, now-timedelta(hours=2) if scenario == 'stale' else now)
    return SimpleNamespace(job=job, attempt=attempt, message=message, raw=raw, prefix=prefix, posted=now)


async def run():
    conn = await asyncpg.connect(checked_dsn())
    for kind in ('json', 'jsonb'):
        await conn.set_type_codec(kind, schema='pg_catalog', encoder=json.dumps, decoder=json.loads)
    gateway = os.environ.get('HERMES_TEST_URL', 'http://hermes-real:8642')
    headers = {'Authorization':'Bearer isolated-real-framework'}
    results = []
    try:
        async with httpx.AsyncClient(timeout=600, trust_env=False) as http:
            for scenario in os.environ.get('INTEGRATION_SCENARIOS', 'text,image,multiple,none,stale').split(','):
                data = await seed(conn, scenario)
                session = f'expert-review-{data.job}-{data.attempt}'
                try:
                    text = (f'review_id: {data.job}\nattempt_id: {data.attempt}\n审核模式: real_time\n'
                            f'发布时间 UTC: {data.posted.isoformat()}\n'
                            f'仅查询联赛 {data.prefix}，这是合成隔离验收，不是实际推荐。\n原始消息：{data.raw}\n'
                            '请读取 expert-review skill，并按系统赛事和盘口核实。')
                    parts = [{'type':'text','text':text}]
                    if scenario == 'image':
                        parts.append({'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(recommendation_image()).decode(), 'detail':'high'}})
                    response = await http.post(gateway+'/v1/chat/completions', headers=headers|{'X-Hermes-Session-Id':session},
                        json={'model':'hermes-agent','messages':[{'role':'system','content':SYSTEM_PROMPT},{'role':'user','content':parts}], 'stream':False})
                    response.raise_for_status()
                    payload = response.json()
                    finish = payload['choices'][0]['finish_reason']
                    signals = await conn.fetch('SELECT fixture_id,line,selection FROM v2_expert_signals WHERE message_id=$1 ORDER BY fixture_id',data.message)
                    traces = await conn.fetch("SELECT trace_kind,payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind LIKE 'mcp_%' ORDER BY sequence_no",data.job)
                    expected = 2 if scenario == 'multiple' else 1 if scenario in {'text','image'} else 0
                    result = {'scenario':scenario,'provider_is_fixture':False,'synthetic_input':True,'finish_reason':finish,
                              'expected_accepted':expected,'accepted':len(signals),'tool_kinds':sorted({r['trace_kind'] for r in traces}),
                              'all_tool_calls_evidenced':all(r['payload'].get('status')=='succeeded' for r in traces)}
                    results.append(result)
                    print(json.dumps(result,ensure_ascii=False),flush=True)
                    assert finish == 'stop' and len(signals)==expected, result
                    if expected:
                        assert {r['fixture_id'] for r in signals} == {data.prefix+'-'+str(i) for i in range(expected)}, result
                        by_fixture = {r['fixture_id']: r for r in signals}
                        assert by_fixture[data.prefix+'-0']['selection']=='over' and Decimal(by_fixture[data.prefix+'-0']['line'])==Decimal('2.5'), result
                        if scenario == 'multiple':
                            assert by_fixture[data.prefix+'-1']['selection']=='under' and Decimal(by_fixture[data.prefix+'-1']['line'])==Decimal('3.5'), result
                        assert set(result['tool_kinds']) == {'mcp_search_matches','mcp_resolve_match','mcp_list_match_markets','mcp_record_expert_signals'}, result
                    if scenario == 'stale':
                        rejected = [r['payload'] for r in traces if r['trace_kind']=='mcp_record_expert_signals']
                        stale_reads = await conn.fetchval(
                            "SELECT count(*) FROM v2_review_market_reads WHERE job_id=$1 AND source_updated_at<=now()-interval '600 seconds'",
                            data.job)
                        assert stale_reads > 0 and 'mcp_list_match_markets' in result['tool_kinds'], result
                        if rejected:
                            assert all(row.get('status')=='succeeded' for row in rejected), result
                            assert any(item.get('reason')=='quote_inactive_unknown_or_stale'
                                       for row in rejected for item in row.get('rejected',[])), result
                    assert result['all_tool_calls_evidenced'], result
                finally:
                    await http.delete(gateway+'/api/sessions/'+session,headers=headers)
        print(json.dumps({'passed':len(results),'results':results},ensure_ascii=False),flush=True)
    finally:
        await conn.close()


if __name__ == '__main__':
    asyncio.run(run())
