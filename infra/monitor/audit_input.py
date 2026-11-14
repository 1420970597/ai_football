"""Prepare a single private audit inside the API container, without DB writes."""
import asyncio
import base64
from datetime import datetime,timedelta,timezone
import hashlib
import json
import sys
import asyncpg
import httpx
from app import blobstore
from app.config import load_settings
from app.review_tool_evidence import replay_snapshot


async def prepare(value):
    m=value['message']
    if datetime.fromisoformat(m['posted_at'])+timedelta(hours=72)<=datetime.now(timezone.utc):
        raise ValueError('expired_original')
    traces=m['traces'];evidence=[]
    for t in traces:
        p=t['payload'];manifest=p.get('response_snapshot')
        if not manifest:continue
        chunks=[x['payload']for x in traces if x['trace_kind']=='tool_response_chunk'
                and x['payload'].get('call_id')==manifest['call_id']]
        wire=replay_snapshot(manifest,chunks)
        evidence.append({'kind':t['trace_kind'],'parameters':p.get('parameters'),'wire':wire})
    signals=[]
    for signal in m.get('signals')or[]:
        signals.append({k:signal.get(k)for k in ('fixture_id','period','market_kind','line','selection','evidence_text')})
    request=next((t['payload']for t in reversed(traces)if t['trace_kind']=='hermes_request'),{})
    reply=request.get('reply_archive')or{}
    reply_images=reply.get('media_objects')or[] if reply.get('media_state')=='ready'and not reply.get('deleted')else[]
    context={'source_text':m['raw_text'],'posted_at':m['posted_at'],'reply_context':m.get('reply_context'),
        'reply_text':request.get('reply_text'),
        'reply_archive':{k:reply.get(k)for k in ('raw_text','posted_at','media_state','deleted')},
        'reply_image_count':len(reply_images),
        'media_state':m['media_state'],'image_count':len(m.get('media_objects')or[]),
        'unsupported_media':m.get('media_manifest')if m['media_state']=='unsupported'else None,
        # The model's narrative is not independent source or tool evidence.
        # Excluding it avoids anchoring this separate audit to its guesses.
        'tool_evidence':evidence,'accepted':signals,
        'state':m.get('state'),'last_error':m.get('last_error')}
    parts=[{'type':'text','text':json.dumps(context,ensure_ascii=False,default=str)}]
    total=0
    for media in (m.get('media_objects')or[])+reply_images:
        image=await blobstore.read_image(str(media['object_key']));total+=len(image)
        if total>24*1024*1024:raise ValueError('image_budget')
        if media.get('sha256')and hashlib.sha256(image).hexdigest()!=media['sha256']:raise ValueError('image_checksum')
        mime=blobstore.image_mime(image)
        parts.append({'type':'image_url','image_url':{'url':'data:'+mime+';base64,'+base64.b64encode(image).decode(),'detail':'high'}})
    unsupported=[]
    if m['media_state']=='unsupported':
        settings=load_settings()
        conn=await asyncpg.connect(settings.database_url,timeout=5,command_timeout=5,
            server_settings={'default_transaction_read_only':'on','statement_timeout':'5000'})
        try:chat=await conn.fetchval('SELECT platform_chat_id FROM v2_expert_sources WHERE id=$1',__import__('uuid').UUID(m['source_id']))
        finally:await conn.close(timeout=3)
        async with httpx.AsyncClient(trust_env=False,timeout=25,
            headers={'Authorization':'Bearer '+settings.collector_token})as client:
            for media in (m.get('media_manifest')or[])[:4]:
                r=await client.post('http://collector:8092/source-info',json={
                    'chat_id':chat,'inspect_media_message_id':media['message_id']})
                r.raise_for_status();item=r.json()
                unsupported.append(item)
    return {'parts':parts,'unsupported_media':unsupported}


if __name__=='__main__':
    print(json.dumps(asyncio.run(prepare(json.load(sys.stdin))),ensure_ascii=False))
