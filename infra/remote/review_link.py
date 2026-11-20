"""Independent strict-host-key SSH transport for the database-local tools."""
import http.client
import json
import os
from pathlib import Path
import re
import sys


def command(config):
    if (set(config) != {'ssh_config', 'ssh_host'}
            or config['ssh_config'] != '/run/storage/ssh_config'
            or not isinstance(config['ssh_host'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,62}', config['ssh_host'])):
        raise ValueError('review_link_configuration_invalid')
    return ['ssh', '-F', config['ssh_config'], '-NT', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
            '-o', 'ConnectTimeout=5', '-o', 'LogLevel=ERROR',
            '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
            '-L', '0.0.0.0:18094:127.0.0.1:18094', config['ssh_host']]


def health():
    connection = http.client.HTTPConnection('127.0.0.1', 18094, timeout=2)
    try:
        connection.request('GET', '/readyz')
        response = connection.getresponse()
        body = response.read(4097)
        if response.status != 200 or len(body) > 4096:
            return 1
        data = json.loads(body)
        return 0 if isinstance(data, dict) and data.get('status') == 'ready' and data.get('role') == 'mcp' else 1
    except (OSError, ValueError, http.client.HTTPException):
        return 1
    finally:
        connection.close()


def main():
    try:
        if sys.argv[1:] == ['--health']:
            return health()
        if sys.argv[1:]:
            raise ValueError('review_link_arguments_invalid')
        config = json.loads(Path('/run/review-tools/link.json').read_text())
        args = command(config)
        # SSH becomes PID 1: Docker forwards termination directly and supervises
        # reconnects. No Python child/thread or shared multiplexed transport.
        os.execvp(args[0], args)
    except Exception:
        print('review_link_unavailable', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
