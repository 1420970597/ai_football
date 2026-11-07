"""Collect bounded Android readiness evidence without treating missing devices as success."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--adb', default='/opt/android-sdk/platform-tools/adb')
parser.add_argument('--host', default='127.0.0.1')
parser.add_argument('--port', default='5037')
parser.add_argument('--serial')
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
report = {'scope': 'android_device_preflight', 'checked_at': datetime.now(timezone.utc).isoformat(),
          'real_environment': False, 'kvm_available': Path('/dev/kvm').exists(), 'status': 'blocked'}
try:
    base = [args.adb, '-H', args.host, '-P', args.port]
    if args.host in {'127.0.0.1', 'localhost'}:
        subprocess.run([args.adb, 'start-server'], capture_output=True, text=True, timeout=10, check=False)
    result = subprocess.run(base + ['devices'], capture_output=True, text=True, timeout=10)
    # Device identifiers and daemon diagnostics are not stored; only state counts.
    states = [line.split()[1] for line in result.stdout.splitlines()[1:] if len(line.split()) >= 2]
    report.update({'adb_exit_code': result.returncode, 'authorized_devices': states.count('device'),
                   'unauthorized_devices': states.count('unauthorized'), 'offline_devices': states.count('offline')})
    if result.returncode:
        report['reason'] = 'adb_connection_failed'
    elif 'device' not in states:
        report['reason'] = 'no_authorized_device'
    elif not args.serial:
        report['reason'] = 'explicit_device_selection_required'
    else:
        boot = subprocess.run(base + ['-s', args.serial, 'shell', 'getprop', 'sys.boot_completed'],
                              capture_output=True, text=True, timeout=10)
        report['boot_completed'] = boot.returncode == 0 and boot.stdout.strip() == '1'
        if report['boot_completed']:
            report.update({'status': 'ready_for_ui_tests', 'reason': None})
        else:
            report['reason'] = 'device_boot_incomplete'
except (OSError, subprocess.TimeoutExpired) as exc:
    report['reason'] = 'adb_timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'adb_unavailable'
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
print(json.dumps(report, sort_keys=True))
raise SystemExit(0 if report['status'] == 'ready_for_ui_tests' else 2)
