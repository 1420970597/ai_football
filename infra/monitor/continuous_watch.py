"""A single durable sampling lane. Only audited, hash-bound windows can pass.

Run in the project's existing Docker toolchain container. No business writes,
model claims, messages or timestamps are synthesized here.
"""
from datetime import datetime,timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from consecutive_windows import record,repaired
from save_window import save


def atomic(path,value):
    temporary=path.with_suffix('.tmp')
    with temporary.open('w')as stream:
        os.fchmod(stream.fileno(),0o600);json.dump(value,stream,ensure_ascii=False,indent=2)
        stream.flush();os.fsync(stream.fileno())
    temporary.replace(path)


def main():
    os.umask(0o077)
    source=Path(os.environ['EXPERT_SOURCE_ROOT']);root=Path(os.environ['EVIDENCE_DIR'])
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=(root/'watch.lock').open('a+')
    fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    first=int(os.environ['FIRST_WINDOW_START'])
    if first%1800:raise ValueError('Unaligned first window')
    statefile=root/'acceptance-state.json'
    state=json.loads(statefile.read_text())if statefile.exists()else{'streak':0,'windows':{}}
    while not state.get('complete'):
        now=int(time.time())
        failure=None
        for repairfile in sorted(root.glob('repair-*.json')):
            repair=json.loads(repairfile.read_text())
            if type(repair.get('at'))is not int or not isinstance(repair.get('revision'),str):
                raise ValueError('Invalid repair marker')
            if repair['at']>state.get('repair_at',0):
                state=repaired(state,revision=repair['revision'],at=repair['at'])
                atomic(statefile,state)
        for end in range(first+1800,now-30+1,1800):
            directory=root/str(end-1800);summaryfile=directory/'snapshot-summary.json'
            if not summaryfile.exists():
                script=(source/'infra/monitor/window_snapshot.py').read_bytes()
                try:
                    result=subprocess.run(['docker','exec','-i','-e',
                        'WINDOW_END='+datetime.fromtimestamp(end,timezone.utc).isoformat(),
                        'ai-football-v2-api-1','python','-'],input=script,
                        capture_output=True,timeout=420,check=False)
                    if result.returncode!=0:raise RuntimeError('snapshot_exit_'+str(result.returncode))
                    summary=save(root,result.stdout)
                except (Exception,)as exc:
                    failure={'state':'snapshot_failed','window_end':end,
                             'reason':type(exc).__name__,'at':now,'streak':0}
                    state=dict(state,streak=0,complete=False);atomic(statefile,state)
                    break
        # Capture later windows even while an earlier human/agent audit is
        # pending, but never advance acceptance beyond that missing audit.
        for end in range(first+1800,now-30+1,1800):
            directory=root/str(end-1800);summaryfile=directory/'snapshot-summary.json'
            if not summaryfile.exists():break
            auditfile=directory/'audit.json'
            if not auditfile.exists():break
            evidence=json.loads(auditfile.read_text());summary=json.loads(summaryfile.read_text())
            if evidence.get('snapshot_sha256')!=summary['snapshot_sha256']:
                failure={'state':'audit_hash_mismatch','window_end':end,'at':now}
                break
            evidence['audit_sha256']=hashlib.sha256(auditfile.read_bytes()).hexdigest()
            state=record(state,evidence);atomic(statefile,state)
        atomic(root/'watch-status.json',failure or {'state':'completed'if state.get('complete')else'watching',
            'at':int(time.time()),'streak':state.get('streak',0),'last_end':state.get('last_end'),
            'next_window_end':(int(time.time())//1800+1)*1800})
        # Raw retention uses original posted_at, never file mtime or edit time.
        for manifestfile in root.glob('*/snapshot-*.private.json.manifest.json'):
            manifest=json.loads(manifestfile.read_text())
            if datetime.fromisoformat(manifest['expires_at']).timestamp()<=time.time():
                for name in manifest['files']:
                    path=manifestfile.parent/name
                    if path.parent!=manifestfile.parent:raise ValueError('Invalid evidence path')
                    path.unlink(missing_ok=True)
                manifestfile.unlink()
        if not state.get('complete'):time.sleep(15)
    return state


if __name__=='__main__':
    print(json.dumps(main()))
