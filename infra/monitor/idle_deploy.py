"""Apply an already-rendered candidate immediately after an idle gate opens.

Run in the project Docker toolchain. Render private Compose before calling;
this coordinator never prints its config or evaluates source inside a shell.
"""
import argparse
import json
from pathlib import Path
import select
import subprocess
import time


def deploy(config,gate_script,release,services,log_path):
    if not services or set(services)-{'api','local-mcp-api'}:
        raise ValueError('only prepared API handoffs are supported')
    if release.parent!=Path('/tmp') or not release.name.startswith('tg-idle-'):
        raise ValueError('unique remote release marker required')
    config=config.resolve(strict=True);gate_script=gate_script.resolve(strict=True)
    gate=subprocess.Popen(['docker','exec','-i','ai-football-v2-review-worker-1',
        'python','-','--release-file',str(release)],stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    held=False;started=None;operation_error=None;gate_code=None
    try:
        gate.stdin.write(gate_script.read_bytes());gate.stdin.close()
        until=time.monotonic()+50
        while time.monotonic()<until:
            if not select.select([gate.stdout],[],[],min(1,max(0,until-time.monotonic())))[0]:
                continue
            line=gate.stdout.readline()
            if not line:break
            event=json.loads(line)
            if event.get('event')=='idle_gate_held':
                held=True;started=time.monotonic();break
        if not held:raise RuntimeError('idle_gate_not_acquired')
        with log_path.open('wb')as log:
            result=subprocess.run(['docker','compose','--project-name','ai-football-v2',
                '--project-directory','/root/ai_football','-f',str(config),'up','-d',
                '--no-deps','--pull','never',*services],stdout=log,stderr=subprocess.STDOUT,
                timeout=12,check=False)
        if result.returncode:raise RuntimeError('candidate_deploy_failed')
    except Exception as exc:
        operation_error=exc
    finally:
        # The remote gate has its own 45-second lifetime as a final fallback.
        # Always release on deployment failure; never leave claims paused.
        released=subprocess.run(['docker','exec','ai-football-v2-review-worker-1',
            'touch',str(release)],capture_output=True,timeout=4,check=False)
        try:gate_code=gate.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gate.terminate();gate_code=gate.wait(timeout=3)
        for stream in (gate.stdout,gate.stderr):stream.close()
        if released.returncode and operation_error is None:
            operation_error=RuntimeError('gate_release_failed')
    if operation_error is not None:raise operation_error
    if gate_code:raise RuntimeError('gate_handoff_failed')
    return {'idle_gate_held':held,'gate_exit_code':gate_code,
            'held_seconds':round(time.monotonic()-started,3),'services':services}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--gate-script',type=Path,required=True)
    parser.add_argument('--release',type=Path,required=True)
    parser.add_argument('--log',type=Path,required=True)
    parser.add_argument('services',nargs='+')
    args=parser.parse_args()
    print(json.dumps(deploy(args.config,args.gate_script,args.release,args.services,args.log)))
