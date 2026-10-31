"""One authenticated SSH connection; fixed project socket and control target only."""
from __future__ import annotations
import json
import http.client
import os
from pathlib import Path
import signal
import subprocess
import threading
import sys

REMOTE_SOCKET = '/root/ai_football/.runtime/remote/api.sock'
STATE = Path('/tmp/remote-link-state.json')


def health(*, state_path=STATE, control_port=18092, token=None) -> int:
    """Check this SSH child and the authenticated forwarded control endpoint."""
    connection = None
    try:
        state = json.loads(state_path.read_text())
        if type(state.get('pid')) is not int or state['pid'] < 1:
            return 1
        os.kill(state['pid'], 0)
        secret = token if token is not None else os.environ.get('COLLECTOR_INTERNAL_TOKEN', '')
        if not secret:
            return 1
        connection = http.client.HTTPConnection('127.0.0.1', control_port, timeout=3)
        connection.request('POST', '/status', body=b'{}', headers={
            'Authorization':'Bearer '+secret, 'Content-Type':'application/json'})
        response = connection.getresponse()
        data = response.read(65537)
        return 0 if response.status == 200 and len(data) <= 65536 and isinstance(json.loads(data), dict) else 1
    except (OSError, ValueError, http.client.HTTPException):
        return 1
    finally:
        if connection:
            connection.close()


def command(config: dict) -> list[str]:
    if set(config) != {'ssh_config', 'ssh_host'}:
        raise ValueError('remote_link_configuration_invalid')
    if config['ssh_config'] != '/run/storage/ssh_config' or not isinstance(config['ssh_host'], str) or not config['ssh_host'].replace('-', '').isalnum():
        raise ValueError('remote_link_target_invalid')
    return ['ssh', '-F', config['ssh_config'], '-NT', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            '-o', 'ConnectTimeout=10', '-o', 'LogLevel=ERROR',
            '-o', 'StreamLocalBindUnlink=yes',
            '-R', REMOTE_SOCKET + ':api:8080',
            '-L', '0.0.0.0:18092:127.0.0.1:18092', config['ssh_host']]


def run(config: dict) -> int:
    process = subprocess.Popen(command(config), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    STATE.write_text(json.dumps({'pid': process.pid}))
    STATE.chmod(0o600)
    stopped = threading.Event()
    def terminate(*_):
        stopped.set()
        process.terminate()
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        while not stopped.wait(1):
            if process.poll() is not None:
                print('remote_link_disconnected', flush=True)
                return 1
    finally:
        STATE.unlink(missing_ok=True)
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    return 0


if __name__ == '__main__':
    try:
        result = (health() if sys.argv[1:] == ['--health'] else
                  run(json.loads(Path(os.environ.get('REMOTE_LINK_CONFIG', '/run/remote/link.json')).read_text())))
    except Exception:
        print('remote_link_configuration_unavailable', flush=True)
        result = 1
    raise SystemExit(result)
