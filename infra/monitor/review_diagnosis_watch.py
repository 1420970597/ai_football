"""Persistent read-only half-hour diagnosis; no trades or model conclusions."""
from __future__ import annotations
import asyncio
from collections import Counter
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

PERIOD = 1800


def summarize(rows):
    from app.review_diagnostics import diagnosis
    reasons=Counter(); states=Counter(); issues=Counter(); rejections=Counter(); examples=[]; delays={}
    for item in rows:
        row=dict(item); code=diagnosis(row)['code']; reasons[code]+=1
        states[row.get('state') or 'not_reviewed']+=1
        rejections.update(row.get('tool_rejections') or {})
        issues.update(k for k,v in (row.get('summary') or {}).get('completion_issues',{}).items()
                      if v and k not in {'detail_limit'})
        for key in ('queue_ms','first_tool_ms','event_to_signal_ms','prompt_tokens'):
            value=row.get(key)
            if value is not None:delays.setdefault(key,[]).append(float(value))
        if diagnosis(row)['requires_attention'] and len(examples)<20:
            examples.append({'message_ref':hashlib.sha256(str(row['id']).encode()).hexdigest()[:12],
                'code':code,'accepted_count':int(row.get('accepted_signal_count') or 0)})
    def quantiles(values):
        values=sorted(values)
        return {'samples':len(values),'p50':round(values[(len(values)-1)//2],3),
            'p95':round(values[int((len(values)-1)*.95)],3),'max':round(values[-1],3)}
    return {'messages':len(rows),'states':dict(states),'diagnosis':dict(reasons),
        'completion_issues':dict(issues),'tool_rejections':dict(rejections),
        'timing':{k:quantiles(v) for k,v in delays.items()},
        'attention_refs':examples,'raw_content_exported':False,'business_writes':0,
        'model_conclusion_is_not_independent_audit':True,'semantic_omission_audit_complete':False}


async def sample(end):
    import asyncpg
    from app.config import load_settings
    conn=await asyncpg.connect(load_settings().database_url,command_timeout=20,
        server_settings={'default_transaction_read_only':'on','statement_timeout':'15000',
                         'application_name':'expert-review-diagnosis-readonly'})
    for typ in ('json','jsonb'):
        await conn.set_type_codec(typ,schema='pg_catalog',encoder=json.dumps,decoder=json.loads)
    try:
        result={'at':datetime.now(timezone.utc).isoformat(),'window_end':end.isoformat(),
                'schema':'review-diagnosis-v1'}
        for label,length in (('30m',timedelta(minutes=30)),('6h',timedelta(hours=6))):
            rows=await conn.fetch(
                "WITH messages AS MATERIALIZED(SELECT DISTINCT ON(source_id,platform_message_id) "
                "id,source_id,platform_message_id,created_at,detected_at FROM v2_expert_messages m "
                "WHERE created_at >= $1 AND created_at < $2 "
                "AND NOT EXISTS(SELECT 1 FROM v2_expert_messages n WHERE n.source_id=m.source_id "
                "AND n.platform_message_id=m.platform_message_id AND n.revision>m.revision) "
                "ORDER BY source_id,platform_message_id,revision DESC) "
                "SELECT m.id,j.state,j.last_error,r.payload summary,"
                "(SELECT count(*) FROM v2_expert_signals s WHERE s.message_id=m.id) accepted_signal_count,"
                "EXISTS(SELECT 1 FROM v2_analysis_traces t WHERE t.job_id=j.id AND trace_kind='mcp_search_matches' "
                "AND t.payload->>'attempt_id'=j.attempt_id::text) has_searches,"
                "EXISTS(SELECT 1 FROM v2_analysis_traces t WHERE t.job_id=j.id AND trace_kind='mcp_list_match_markets' "
                "AND t.payload->>'attempt_id'=j.attempt_id::text) has_market_reads,"
                "EXISTS(SELECT 1 FROM v2_analysis_traces t WHERE t.job_id=j.id AND trace_kind='hermes_result' "
                "AND t.payload->>'attempt_id'=j.attempt_id::text) has_model_result,"
                "(SELECT (payload->>'event_to_claim_ms')::numeric FROM v2_analysis_traces t WHERE t.job_id=j.id "
                "AND trace_kind='attempt_claimed' AND payload->>'attempt_id'=j.attempt_id::text "
                "ORDER BY sequence_no DESC LIMIT 1) queue_ms,"
                "(SELECT extract(epoch FROM (min(t.created_at)-m.detected_at))*1000 FROM v2_analysis_traces t "
                "WHERE t.job_id=j.id AND t.trace_kind LIKE 'mcp_%' AND t.payload->>'attempt_id'=j.attempt_id::text) first_tool_ms,"
                "(SELECT extract(epoch FROM (min(s.created_at)-m.detected_at))*1000 FROM v2_expert_signals s "
                "WHERE s.message_id=m.id) event_to_signal_ms,"
                "(SELECT (payload->'usage'->>'prompt_tokens')::numeric FROM v2_analysis_traces t "
                "WHERE t.job_id=j.id AND trace_kind='hermes_result' AND payload->>'attempt_id'=j.attempt_id::text "
                "ORDER BY sequence_no DESC LIMIT 1) prompt_tokens,"
                "(SELECT jsonb_object_agg(reason,n) FROM (SELECT "
                "coalesce(t.payload->>'rejection_reason','legacy_unspecified') reason,count(*) n "
                "FROM v2_analysis_traces t WHERE t.job_id=j.id AND t.trace_kind LIKE 'mcp_%' "
                "AND t.payload->>'status'='failed' AND t.payload->>'attempt_id'=j.attempt_id::text "
                "GROUP BY 1) failures) tool_rejections "
                "FROM messages m LEFT JOIN LATERAL(SELECT id,state,last_error,attempt_id FROM v2_analysis_jobs "
                "WHERE message_id=m.id ORDER BY created_at DESC,id DESC LIMIT 1)j ON TRUE "
                "LEFT JOIN LATERAL(SELECT payload FROM v2_analysis_traces t WHERE t.job_id=j.id "
                "AND trace_kind='system_review_result' AND t.payload->>'attempt_id'=j.attempt_id::text "
                "ORDER BY sequence_no DESC LIMIT 1)r ON TRUE",end-length,end)
            result[label]=summarize(rows)
        return result
    finally:await conn.close()


def atomic(path,value):
    temp=path.with_suffix('.tmp')
    with temp.open('w')as stream:
        os.fchmod(stream.fileno(),0o600);json.dump(value,stream,ensure_ascii=False,indent=2)
        stream.flush();os.fsync(stream.fileno())
    temp.replace(path)


def watch():
    os.umask(0o077)
    root=Path(os.environ['EVIDENCE_DIR']);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    import fcntl
    lock=(root/'watch.lock').open('a+');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    source=Path(__file__).read_bytes();last=None
    while True:
        end=int(time.time()-30)//PERIOD*PERIOD
        if end!=last:
            target=root/(str(end)+'.json')
            if not target.exists():
                try:
                    stamp=datetime.fromtimestamp(end,timezone.utc).isoformat()
                    run=subprocess.run(['docker','exec','-i','-e','DIAGNOSIS_SAMPLE_END='+stamp,
                        'ai-football-v2-api-1','python','-','--sample'],input=source,
                        capture_output=True,timeout=50,check=False)
                    if run.returncode:raise RuntimeError('sample_exit_'+str(run.returncode))
                    value=json.loads(run.stdout);atomic(target,value)
                    atomic(root/'status.json',{'state':'watching','at':int(time.time()),'last_end':end,
                        'next_end':end+PERIOD,'last_snapshot':target.name,'interval_seconds':PERIOD})
                    print(json.dumps({'window_end':end,'messages':value['30m']['messages'],
                        'diagnosis':value['30m']['diagnosis']}),flush=True)
                except Exception as exc:
                    atomic(root/'status.json',{'state':'sample_failed','at':int(time.time()),
                        'window_end':end,'reason':type(exc).__name__})
                    time.sleep(30);continue
            last=end
        time.sleep(15)


if __name__=='__main__':
    if '--sample'in sys.argv:
        print(json.dumps(asyncio.run(sample(datetime.fromisoformat(os.environ['DIAGNOSIS_SAMPLE_END']))),default=str))
    elif '--health'in sys.argv:
        status=json.loads((Path(os.environ['EVIDENCE_DIR'])/'status.json').read_text())
        sys.exit(0 if status['state']=='watching' and time.time()-status['last_end']<PERIOD+120 else 1)
    else:watch()
