"""Supervised SSH transports with fixed project socket and control targets."""
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
        pids = state.get('pids', [state.get('pid')])
        if (not isinstance(pids, list) or len(pids) not in (1, 2)
                or any(type(pid) is not int or pid < 1 for pid in pids)
                or len(set(pids)) != len(pids)):
            return 1
        for pid in pids:
            os.kill(pid, 0)
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
    if (set(config) not in ({'ssh_config', 'ssh_host'}, {'ssh_config', 'ssh_host', 'split_connections'})
            or type(config.get('split_connections', False)) is not bool):
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


def commands(config: dict) -> list[list[str]]:
    combined = command(config)
    if not config.get('split_connections', False):
        return [combined]
    prefix = combined[:combined.index('-R')]
    # A config-level ControlMaster must not collapse these into one TCP stream.
    prefix += ['-o', 'ControlMaster=no', '-o', 'ControlPath=none']
    return [[*prefix, '-R', REMOTE_SOCKET + ':api:8080', config['ssh_host']],
            [*prefix, '-L', '0.0.0.0:18092:127.0.0.1:18092', config['ssh_host']]]


def run(config: dict, *, state_path=STATE) -> int:
    planned = commands(config)
    processes = []
    stopped = threading.Event()
    def terminate(*_):
        stopped.set()
    old_handlers = {}
    temporary = state_path.with_suffix('.pending')
    try:
        for name in (signal.SIGTERM, signal.SIGINT):
            old_handlers[name] = signal.signal(name, terminate)
        for args in planned:
            if stopped.is_set():
                return 0
            processes.append(subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        pids = [process.pid for process in processes]
        with open(temporary, 'w', opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
            json.dump({'pid': pids[0], 'pids': pids}, output)
        temporary.chmod(0o600)
        temporary.replace(state_path)
        while not stopped.wait(1):
            if any(process.poll() is not None for process in processes):
                print('remote_link_disconnected', flush=True)
                return 1
    finally:
        state_path.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for name, previous in old_handlers.items():
            signal.signal(name, previous)
    return 0


if __name__ == '__main__':
    try:
        result = (health() if sys.argv[1:] == ['--health'] else
                  run(json.loads(Path(os.environ.get('REMOTE_LINK_CONFIG', '/run/remote/link.json')).read_text())))
    except Exception:
        print('remote_link_configuration_unavailable', flush=True)
        result = 1
    raise SystemExit(result)
