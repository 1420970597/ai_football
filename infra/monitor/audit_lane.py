"""Reuse imports per audit lane; every request still creates a fresh agent."""
import json
import selectors
import subprocess
import threading

LOCAL = threading.local()


def worker_source(source):
    video=(source/'infra/monitor/audit_video.py').read_text()
    review=(source/'infra/monitor/independent_review.py').read_text().split("if __name__=='__main__':",1)[0]
    return video+chr(10)+review+'''
import os, signal
from gateway.run import _resolve_runtime_agent_kwargs, _resolve_gateway_model
from run_agent import AIAgent
def expired(signum, frame):
    os._exit(124)
signal.signal(signal.SIGALRM, expired)
signal.alarm(60)
print('{"ready":true}', flush=True)
for line in sys.stdin:
    signal.alarm(60)
    try:
        value=run(prepare(json.loads(line)))
        print(json.dumps(value), flush=True)
    except Exception as exc:
        print(json.dumps({'audit_error':type(exc).__name__}), flush=True)
    finally:
        signal.alarm(60)
'''


class AuditLane:
    def __init__(self, source, command):
        self.source=source
        self.command=command
        self.process=None

    def close(self):
        process=self.process
        self.process=None
        if process is None:return
        if process.stdin:process.stdin.close()
        try:process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=3)
        if process.stdout:process.stdout.close()

    def read(self, seconds):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout,selectors.EVENT_READ)
            if not selector.select(seconds):
                self.close();raise RuntimeError('audit_lane_deadline')
        line=self.process.stdout.readline()
        if not line:
            self.close();raise RuntimeError('audit_lane_closed')
        return json.loads(line)

    def review(self, prepared):
        if self.process is None or self.process.poll() is not None:
            self.close()
            command=self.command()
            command[command.index('60')]='1200'
            self.process=subprocess.Popen(command+[worker_source(self.source)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)
            if self.read(65)!={'ready':True}:
                self.close();raise RuntimeError('audit_lane_not_ready')
        self.process.stdin.write(prepared.replace(bytes([10]),b' ')+bytes([10]))
        self.process.stdin.flush()
        value=self.read(65)
        if 'audit_error' in value:raise RuntimeError('review_failed')
        return value
