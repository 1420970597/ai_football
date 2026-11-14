"""Run in the API container. Stdout is private original-post TTL evidence."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import uuid

import asyncpg
import httpx
from app.config import load_settings


async def snapshot():
    end=datetime.fromisoformat(os.environ['WINDOW_END'].replace('Z','+00:00'))
    start=end-timedelta(minutes=30)
    if end>datetime.now(timezone.utc):raise ValueError('Window has not closed')
    settings=load_settings()
    conn=await asyncpg.connect(settings.database_url,timeout=5,command_timeout=10,
        server_settings={'default_transaction_read_only':'on','statement_timeout':'10000',
                         'application_name':'tg-window-coverage-readonly'})
    await conn.set_type_codec('jsonb',encoder=json.dumps,decoder=json.loads,schema='pg_catalog')
    try:
        sources=await conn.fetch(
            'SELECT s.id,s.platform_chat_id,count(e.id) FILTER(WHERE e.enabled AND se.enabled) trusted '
            'FROM v2_expert_sources s LEFT JOIN v2_source_experts se ON se.source_id=s.id '
            'LEFT JOIN v2_experts e ON e.id=se.expert_id WHERE s.enabled GROUP BY s.id ORDER BY s.id')
        source_evidence=[]
        async with httpx.AsyncClient(trust_env=False,timeout=90,
            headers={'Authorization':'Bearer '+settings.collector_token}) as client:
            health=await client.post('http://collector:8092/status',json={});health.raise_for_status()
            runtime=health.json()
            for source in sources:
                ref=hashlib.sha256(str(source['id']).encode()).hexdigest()[:12]
                if not source['trusted']:
                    source_evidence.append({'source_ref':ref,'excluded':'no_trusted_author'});continue
                cursor=None;seen=set();items=[];complete=False;reason=None
                for _ in range(20):
                    body={'chat_id':source['platform_chat_id'],'window_start':start.isoformat(),
                          'window_end':end.isoformat()}
                    if cursor is not None:body['cursor']=cursor
                    try:
                        response=await client.post('http://collector:8092/source-info',json=body)
                        response.raise_for_status();page=response.json()
                    except (httpx.HTTPError,ValueError) as exc:
                        reason=type(exc).__name__;break
                    if page.get('state')!='ready':reason=page.get('reason')or page.get('state');break
                    batch=page.get('messages')
                    if not isinstance(batch,list):reason='invalid_upstream_shape';break
                    for item in batch:
                        if item['message_id']not in seen:seen.add(item['message_id']);items.append(item)
                    if page.get('complete')is True:complete=True;break
                    following=page.get('next_cursor')
                    if type(following)is not int or(cursor is not None and following>=cursor):
                        reason='invalid_pagination';break
                    cursor=following
                required=[m for m in items if m.get('trusted')and m.get('kind')=='Message'
                          and(m.get('has_text')or m.get('has_media'))]
                for item in required:
                    row=await conn.fetchrow(
                        "SELECT ev.message_id FROM v2_telegram_events ev WHERE ev.source_id=$1 AND ev.is_current "
                        "AND(ev.content->'member_message_ids') @> $2::jsonb ORDER BY ev.received_at DESC LIMIT 1",
                        source['id'],[item['message_id']])
                    item['persisted']=row is not None
                    item['stored_message_id']=str(row['message_id'])if row and row['message_id']else None
                source_evidence.append({'source_ref':ref,'complete':complete,'reason':reason,
                    'trusted_messages':len(required),'missing_messages':sum(not m['persisted']for m in required),
                    'messages':required})
        ids=[uuid.UUID(m['stored_message_id'])for s in source_evidence for m in s.get('messages',[])if m.get('stored_message_id')]
        rows=await conn.fetch(
            "SELECT m.id,m.source_id,m.expert_id,m.raw_text,m.reply_context,m.reply_to_platform_message_id,m.media_objects,m.media_state,"
            "m.platform_message_id,m.revision,m.posted_at,m.detected_at,m.deleted_at,m.raw_content_expired_at,"
            "(SELECT ev.content->'media_manifest' FROM v2_telegram_events ev WHERE ev.message_id=m.id "
            "AND ev.is_current ORDER BY ev.received_at DESC LIMIT 1) media_manifest,"
            "j.id job_id,j.state,j.review_mode,j.last_error,j.attempt_id,"
            "(SELECT jsonb_agg(to_jsonb(es)) FROM v2_expert_signals es WHERE es.message_id=m.id) signals "
            "FROM v2_expert_messages m LEFT JOIN v2_analysis_jobs j ON j.message_id=m.id "
            "WHERE m.id=ANY($3::uuid[]) OR(m.detected_at >=$1 AND m.detected_at<$2 "
            "AND m.deleted_at IS NULL AND NOT EXISTS(SELECT 1 FROM v2_expert_messages n "
            "WHERE n.source_id=m.source_id AND n.platform_message_id=m.platform_message_id AND n.revision>m.revision)) "
            "ORDER BY m.detected_at LIMIT 2001",start,end,ids)
        messages=[dict(m)for m in rows];jobs=[m['job_id']for m in messages if m['job_id']]
        traces=await conn.fetch('SELECT job_id,trace_kind,payload,created_at FROM v2_analysis_traces '
            'WHERE job_id=ANY($1::uuid[]) ORDER BY job_id,sequence_no',jobs)
        by={}
        for t in traces:by.setdefault(t['job_id'],[]).append(dict(t))
        for m in messages:m['traces']=by.get(m['job_id'],[])
        return {'start':start,'end':end,'sampled_at':datetime.now(timezone.utc),
                'source_evidence':source_evidence,'messages':messages,'runtime':runtime,
                'database_complete':len(messages)<=2000,'read_only':await conn.fetchval('SHOW transaction_read_only')}
    finally:await conn.close(timeout=5)


if __name__=='__main__':
    print(json.dumps(asyncio.run(snapshot()),ensure_ascii=False,default=str))
