"""Audit closed private windows with bounded Docker-only, read-only work."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from audit_lane import AuditLane, LOCAL

from independent_review import CLASSES, VERDICTS
from window_audit import audit_directory
from continuous_watch import atomic


def review_command(container=None):
    """Semantic audit has its own CPU/memory cgroup and no business tools."""
    target=container or os.environ.get('EXPERT_AUDIT_HERMES_CONTAINER','ai-football-tg-semantic-hermes')
    if target=='ai-football-v2-hermes-1':
        raise ValueError('audit_must_not_share_realtime_hermes')
    if not target or not target[0].isalnum()or any(not(c.isalnum()or c in '_.-')for c in target):
        raise ValueError('invalid_audit_container')
    # Timing out the Docker CLI does not stop its process inside the container.
    # Bound that process group first; leave time for the CLI to collect exit.
    return ['docker','exec','-i','--user','hermes',target,'nice','-n','19',
            'timeout','--signal=TERM','--kill-after=2','60','python','-c']


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
    lane=getattr(LOCAL,'lane',None)
    if lane is not None:
        value=lane.review(prepared.stdout)
    else:
        result=subprocess.run(review_command()+[review],input=prepared.stdout,
            capture_output=True,timeout=68,check=False)
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
    pending=[]
    for message in messages:
        ref=hashlib.sha256(str(message['id']).encode()).hexdigest()[:12]
        target=destination/(ref+'.json')
        if target.exists():continue
        pending.append((message,ref,target))
    errors=[]
    # Two independent reads share the existing isolated 0.30 CPU/768 MiB
    # cgroup. Model network waits overlap; no business concurrency changes.
    lanes=[];lane_guard=threading.Lock()
    def initialize_lane():
        lane=AuditLane(source,review_command);LOCAL.lane=lane
        with lane_guard:lanes.append(lane)
    try:
        with ThreadPoolExecutor(max_workers=2,initializer=initialize_lane) as executor:
            jobs={executor.submit(review_message,source,message):(ref,target)
                  for message,ref,target in pending}
            for job in as_completed(jobs):
                ref,target=jobs[job]
                try:value=job.result()
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    continue
                # Transport failures may retry; valid uncertainty is immutable.
                value.update(message_ref=ref,snapshot_sha256=summary['snapshot_sha256'])
                if target.exists():raise ValueError('audit_result_already_exists')
                atomic(target,value)
                print(json.dumps({'message_ref':ref,'verdict':value['verdict']}),flush=True)
    finally:
        for lane in lanes:lane.close()
    if errors:raise RuntimeError('audit_transport_incomplete')
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
