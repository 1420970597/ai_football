import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SVAY = 'ai-football-profitability-reference-svay'
SVAY_REPLAY = 'ai-football-profitability-reference-replay-svay'
SVAY_LABEL = 'aseanc-svr-por-2026-11-19'


class ReferencePilotRestoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        scripts = self.root / 'infra/profitability'
        scripts.mkdir(parents=True)
        self.script = scripts / 'restore_reference_pilot.sh'
        shutil.copyfile(ROOT / 'infra/profitability' / self.script.name, self.script)
        self.archive = self.root / '.runtime/profitability-watch/public-orderbook-pilot-20261118'
        self.archive.mkdir(parents=True)
        self.bindings = self.archive / 'fixture-bindings-20261119.json'
        self.bindings.write_text('{}\n')
        commands = self.root / 'commands'
        commands.mkdir()
        self.env = dict(os.environ, PATH=f'{commands}:{os.environ["PATH"]}')

        for name, body in {
            'date': 'if [[ $* == "-u +%s" ]]; then echo "$NOW"; else exec /bin/date "$@"; fi',
            'systemctl': '[[ $* == "is-active --quiet $LIVE_UNIT" ]]',
            'systemd-run': 'printf "%s\\t" "$@"; printf "\\n"',
        }.items():
            executable = commands / name
            executable.write_text('#!/usr/bin/env bash\n' + body + '\n')
            executable.chmod(0o700)
        (scripts / 'capture_reference_window.sh').write_text('printf "%s\\n" "$@"\n')

    def invoke(self, now, *args, active=''):
        env = dict(self.env, NOW=str(int(datetime.fromisoformat(now).timestamp())), LIVE_UNIT=active)
        result = subprocess.run(
            ['bash', self.script, *args], env=env, capture_output=True, text=True, timeout=5,
        )
        registrations = [
            line.rstrip('\t').split('\t') for line in result.stdout.splitlines()
            if line.startswith('--unit=')
        ]
        return result, {row[0].removeprefix('--unit='): row for row in registrations}

    def test_future_uses_original_schedule_and_preserves_running_service(self):
        result, units = self.invoke('2026-11-19T11:00:00+00:00', active=f'{SVAY}.service')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn(SVAY, units)
        self.assertIn('--on-calendar=2026-11-19 14:32:00 UTC', units[SVAY_REPLAY])
        vienna = units['ai-football-profitability-reference-vienna']
        self.assertIn('--on-calendar=2026-11-19 17:42:00 UTC', vienna)
        self.assertIn('KXUCLWSPREAD-26NOV19FAKBMU-BMU2', vienna)

    def test_in_progress_uses_remaining_rounds_at_service_launch(self):
        result, units = self.invoke('2026-11-19T12:00:00+00:00')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('--on-calendar=2026-11-19 12:00:05 UTC', units[SVAY])
        capture = ('--capture', '2026-11-19T11:57:00Z', SVAY_LABEL, '--polymarket', SVAY_LABEL + '-svr')
        result, _ = self.invoke('2026-11-19T12:07:00+00:00', *capture)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            [SVAY_LABEL, '--samples', '219', '--interval-seconds', '33', *capture[3:]],
            result.stdout.splitlines(),
        )
        result, _ = self.invoke('2026-11-19T14:07:22+00:00', *capture)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn('--samples', result.stdout)

    def test_elapsed_windows_replay_only_when_usable_archive_exists(self):
        capture = self.archive / f'{SVAY_LABEL}-2026-11-19T11:57:00Z.jsonl'
        capture.touch()
        now = '2026-11-20T02:00:00+00:00'
        result, units = self.invoke(now)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(units)
        capture.write_text('{}\n')
        result, units = self.invoke(now)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({SVAY_REPLAY}, set(units))
        self.assertIn('--on-calendar=2026-11-20 02:00:05 UTC', units[SVAY_REPLAY])
        self.assertEqual(
            [SVAY_LABEL, 'leyu:5860873', '2026-11-19T11:57:00Z', '2026-11-19T14:32:00Z', self.bindings.name],
            units[SVAY_REPLAY][-5:],
        )
        (self.archive / f'{SVAY_LABEL}-replay-2026-11-20T02:00:05Z.json').write_text('{}\n')
        result, units = self.invoke(now)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(units)

    def test_missing_bindings_prevents_registration(self):
        self.bindings.unlink()
        result, units = self.invoke('2026-11-19T11:00:00+00:00')
        self.assertEqual(2, result.returncode)
        self.assertIn('bindings', result.stderr)
        self.assertFalse(units)
