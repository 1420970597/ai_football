"""Bind semantic audits and interval health to one immutable private snapshot."""
from collections import Counter
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess


def interval_health(samples, start, end):
    rows=sorted((s for s in samples if start-90 <= s.get('at',0) <= end+90),key=lambda s:s['at'])
    if not rows or rows[0]['at']>start+90 or rows[-1]['at']<end-90:return False
    if any(b['at']-a['at']>180 for a,b in zip(rows,rows[1:])):return False
    return all(s.get('ok')is True for s in rows if start<=s['at']<end)


def duplicate_count(messages):
    counts=Counter()
    for message in messages:
        for signal in message.get('signals')or[]:
            if signal.get('withdrawn_at') is not None:continue
            baseline=None
            if signal.get('phase')=='live'and signal.get('score_basis')!='full_period':
                score=signal.get('score_baseline')or{}
                fields=('home','away','half_home','half_away')if signal.get('period')=='first_half'else(
                    tuple(sorted(score))if signal.get('period')=='second_half'else('home','away'))
                baseline=tuple((k,score.get(k))for k in fields)
            key=tuple(str(signal.get(k))for k in('expert_id','fixture_id','market_id','market_kind',
                'period','selection','phase','score_basis'))+(Decimal(str(signal.get('line')or 0)),baseline)
            counts[key]+=1
    return sum(n-1 for n in counts.values()if n>1)


def assemble(summary,snapshot,audits,health):
    eligible=[m for m in snapshot['messages']if m.get('review_mode')!='historical_replay'
              and m.get('last_error')!='source_edited']
    checked=[];missing=0;deadlines=0
    for message in eligible:
        ref=hashlib.sha256(str(message['id']).encode()).hexdigest()[:12]
        audit=audits.get(ref)
        if not audit or audit.get('snapshot_sha256')!=summary['snapshot_sha256']:
            missing+=1
        else:checked.append(audit)
        late=message.get('last_error')=='detection_deadline_exceeded'
        for signal in message.get('signals')or[]:
            if message.get('detected_at')and (datetime.fromisoformat(signal['created_at'])-
                datetime.fromisoformat(message['detected_at'])).total_seconds()>20:late=True
        deadlines+=bool(late)
    value={k:summary[k]for k in('start','end','snapshot_sha256','trusted_messages',
        'missing_messages','upstream_complete','database_complete')}
    value.update(source_health_ok=interval_health(health,summary['start'],summary['end']),
        audit_complete=missing==0,unreviewed_messages=missing,
        recommendation_messages=sum(a.get('classification')=='recommendation'for a in checked),
        confirmed_omissions=sum(a.get('verdict')=='confirmed_omission'for a in checked),
        confirmed_misbindings=sum(a.get('verdict')=='confirmed_misbinding'for a in checked),
        duplicate_signals=duplicate_count(eligible),deadline_violations=deadlines,
        unexplained_outcomes=missing+sum(a.get('verdict')not in {
            'consistent','confirmed_omission','confirmed_misbinding'}for a in checked),
        semantic_messages=len(eligible),health_samples=sum(summary['start']<=s.get('at',0)<summary['end']for s in health))
    return value


def audit_directory(directory):
    directory=Path(directory);summary=json.loads((directory/'snapshot-summary.json').read_text())
    body=Path(summary['snapshot_file']).read_bytes()
    if hashlib.sha256(body).hexdigest()!=summary['snapshot_sha256']:raise ValueError('snapshot_changed')
    audits={p.stem:json.loads(p.read_text())for p in(directory/'independent').glob('*.json')}
    health=[json.loads(p.read_text())for p in(directory.parent/'health').glob('*.json')]
    value=assemble(summary,json.loads(body),audits,health)
    # Minute samples can miss a short recovery failure. Inspect only this
    # window's collector log; retain counts, never source or message content.
    running=subprocess.run(['docker','inspect','ai-football-v2-collector-1',
        '--format','{{.State.StartedAt}}'],capture_output=True,timeout=10,check=False)
    try:
        started=datetime.fromisoformat(running.stdout.decode().strip().replace('Z','+00:00')).timestamp()
    except(ValueError,UnicodeError):started=None
    if running.returncode!=0 or started is None or started>summary['start']:
        # Recreating a container discards its predecessor's log stream.
        # An empty new stream cannot prove the older interval was healthy.
        value.update(source_health_ok=False,collector_log_complete=False)
    else:
        result=subprocess.run(['docker','logs','--since',str(summary['start']),
            '--until',str(summary['end']),'ai-football-v2-collector-1'],
            capture_output=True,timeout=15,check=False)
        if result.returncode!=0:
            value.update(source_health_ok=False,collector_log_complete=False)
        else:
            failures=collector_failure_count((result.stdout+result.stderr).decode(errors='replace'))
            value.update(collector_log_complete=True,collector_failures=failures)
            if failures:value['source_health_ok']=False
    return value


def collector_failure_count(log):
    markers=('Telegram source deferred','Telegram source unavailable',
        'Telegram source lease refresh deferred','Telegram session invalidated',
        'Telegram live event deferred','Telegram source backoff',
        'Telegram recent history deferred','Telegram media retry deferred')
    return sum(any(marker in line for marker in markers)for line in log.splitlines())
