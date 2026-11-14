"""Audit closed private windows in a single Docker-only, read-only lane."""
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from independent_review import CLASSES, VERDICTS
from window_audit import audit_directory
from continuous_watch import atomic


def pending_messages(directory):
    summary=json.loads((directory/'snapshot-summary.json').read_text())
    body=Path(summary['snapshot_file']).read_bytes()
    if hashlib.sha256(body).hexdigest()!=summary['snapshot_sha256']:
        raise ValueError('snapshot_changed')
    messages=json.loads(body)['messages']
    if any(datetime.fromisoformat(m['posted_at']).timestamp()+72*3600<=time.time()for m in messages):
        raise ValueError('original_expired')
    return summary,[m for m in messages if m.get('review_mode')!='historical_replay'
        and m.get('last_error')!='source_edited']


def review_message(source,message):
    prep=(source/'infra/monitor/audit_input.py').read_text()
    review=(source/'infra/monitor/audit_video.py').read_text()+'\n'+(
        source/'infra/monitor/independent_review.py').read_text().replace(
            'run(json.load(sys.stdin))','run(prepare(json.load(sys.stdin)))')
    prepared=subprocess.run(['docker','exec','-i','ai-football-v2-api-1','python','-c',prep],
        input=json.dumps({'message':message}).encode(),capture_output=True,timeout=40,check=False)
    if prepared.returncode:raise RuntimeError('prepare_failed')
    result=subprocess.run(['docker','exec','-i','--user','hermes','ai-football-v2-hermes-1',
        'nice','-n','19','python','-c',review],input=prepared.stdout,capture_output=True,timeout=65,check=False)
    if result.returncode:raise RuntimeError('review_failed')
    value=json.loads(result.stdout)
    if value.get('classification')not in CLASSES or value.get('verdict')not in VERDICTS:
        raise ValueError('invalid_audit')
    if value.get('tool_count')!=0 or value.get('persistent_session')is not False:
        raise ValueError('audit_permissions_invalid')
    return value


def audit_window(source,directory):
    if(directory/'audit.json').exists():return
    summary,messages=pending_messages(directory)
    destination=directory/'independent';destination.mkdir(exist_ok=True,mode=0o700)
    for message in messages:
        ref=hashlib.sha256(str(message['id']).encode()).hexdigest()[:12]
        target=destination/(ref+'.json')
        if target.exists():continue
        # Transport failures may be retried; a valid uncertain verdict is kept.
        value=review_message(source,message)
        value.update(message_ref=ref,snapshot_sha256=summary['snapshot_sha256'])
        atomic(target,value)
        print(json.dumps({'message_ref':ref,'verdict':value['verdict']}),flush=True)
    value=audit_directory(directory)
    if not value['audit_complete']:raise ValueError('audit_incomplete')
    atomic(directory/'audit.json',value)
    print(json.dumps({'window_start':summary['start'],'audit_complete':True,
        'unexplained_outcomes':value['unexplained_outcomes']}),flush=True)


def main():
    os.umask(0o077)
    source=Path(os.environ['EXPERT_SOURCE_ROOT']);root=Path(os.environ['EVIDENCE_DIR'])
    lock=(root/'audit.lock').open('a+')
    fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    while True:
        statefile=root/'acceptance-state.json'
        if statefile.exists()and json.loads(statefile.read_text()).get('complete'):return
        for directory in sorted(root.iterdir()):
            if not directory.is_dir()or not directory.name.isdigit():continue
            if not(directory/'snapshot-summary.json').exists()or(directory/'audit.json').exists():continue
            try:audit_window(source,directory)
            except Exception as exc:
                print(json.dumps({'window_start':int(directory.name),
                    'audit_error':type(exc).__name__}),flush=True)
        time.sleep(15)


if __name__=='__main__':main()
