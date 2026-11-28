import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SVAY = 'ai-football-profitability-reference-svay'
SVAY_STREAM = 'ai-football-profitability-reference-stream-svay'
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
            'systemctl': '''
if [[ $1 == is-active ]]; then
    [[ $3 == "$LIVE_UNIT" && $LIVE_STATE == active ]]
elif [[ $1 == show ]]; then
    if [[ $2 == "$LIVE_UNIT" ]]; then echo "$LIVE_STATE"; else echo inactive; fi
fi
''',
            'systemd-run': 'printf "%s\\t" "$@"; printf "\\n"',
            'docker': 'printf "%s\\n" "$CATALOG_MARKET"',
        }.items():
            executable = commands / name
            executable.write_text('#!/usr/bin/env bash\n' + body + '\n')
            executable.chmod(0o700)
        (scripts / 'capture_reference_window.sh').write_text('printf "%s\\n" "$@"\n')

    def invoke(self, now, *args, active='', state='active'):
        env = dict(self.env, NOW=str(int(datetime.fromisoformat(now).timestamp())), LIVE_UNIT=active, LIVE_STATE=state)
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
        stream = units[SVAY_STREAM]
        self.assertIn('--on-calendar=2026-11-19 12:00:00 UTC', stream)
        self.assertIn('--stream', stream)
        self.assertIn(f'{SVAY_LABEL}-spread-home-1pt5', stream)
        adjacent = units['ai-football-profitability-reference-vasco-adjacent']
        self.assertIn('--on-calendar=2026-11-19 22:27:00 UTC', adjacent)
        self.assertEqual(['--polymarket', 'bra-vas-int-2026-11-19-total-2pt5',
                          '--polymarket', 'bra-vas-int-2026-11-19-total-3pt5',
                          '--kalshi', 'KXBRASILEIROTOTAL-26NOV19VDGINT-3',
                          '--kalshi', 'KXBRASILEIROTOTAL-26NOV19VDGINT-4'], adjacent[-8:])

    def test_in_progress_uses_remaining_rounds_at_service_launch(self):
        result, units = self.invoke('2026-11-19T12:00:00+00:00')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('--on-calendar=2026-11-19 12:00:05 UTC', units[SVAY])
        self.assertIn('--on-calendar=2026-11-19 12:00:05 UTC', units[SVAY_STREAM])
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

    def test_benfica_market_and_score_observation_share_the_fixture_window(self):
        result, units = self.invoke('2026-11-19T13:00:00+00:00')
        self.assertEqual(0, result.returncode, result.stderr)
        prefix = 'ai-football-profitability-reference-'
        label = 'uwcl-slb-asr-2026-11-19'
        markets = units[prefix + 'benfica']
        stream = units[prefix + 'stream-benfica']
        score = units[prefix + 'score-benfica']
        replay = units[prefix + 'replay-benfica']
        for unit in (markets, score):
            self.assertIn('--on-calendar=2026-11-19 19:57:00 UTC', unit)
        self.assertIn('--on-calendar=2026-11-19 20:00:00 UTC', stream)
        market_names = [f'{label}-{suffix}' for suffix in (
            'slb', 'draw', 'asr', 'total-2pt5', 'first-half-total-1pt5', 'spread-home-1pt5',
        )]
        self.assertEqual(market_names, [value for value in markets if value.startswith(f'{label}-')])
        self.assertEqual(market_names, stream[-6:])
        self.assertEqual(['401916974', 'BEN', 'ROMA', '--soccer-league', 'uefa.wchampions',
                          '--kickoff', '2026-11-19T20:00:00Z'], score[-7:])
        self.assertIn('--on-calendar=2026-11-19 22:32:00 UTC', replay)
        self.assertEqual([label, 'leyu:5831469', '2026-11-19T19:57:00Z', '2026-11-19T22:32:00Z',
                          self.bindings.name], replay[-5:])
        us = units[prefix + 'us-benfica']
        us_prematch = units[prefix + 'us-benfica-prematch']
        self.assertIn('--on-calendar=2026-11-19 19:57:00 UTC', us)
        self.assertIn('--on-calendar=2026-11-19 17:30:00 UTC', us_prematch)
        self.assertEqual(['--polymarket-us', f'atc-{label}-slb', '--polymarket-us', f'atc-{label}-draw',
                          '--polymarket-us', f'atc-{label}-asr'], us_prematch[-6:])
        self.assertEqual(us[-6:], us_prematch[-6:])

    def test_expansion_has_the_original_window_and_preserves_active_timer(self):
        label = 'uwcl-slb-asr-2026-11-19'
        (self.archive / f'catalog-{label}.json').write_text('{}\n')
        self.env['CATALOG_MARKET'] = f'{label}-total-3pt5'
        unit = 'ai-football-profitability-reference-catalog-benfica'
        result, units = self.invoke('2026-11-19T13:00:00+00:00')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('--on-calendar=2026-11-19 19:57:00 UTC', units[unit])
        self.assertEqual(['--capture', '2026-11-19T19:57:00Z', f'{label}-catalog',
                          '--polymarket-event', f'{label}-more-markets',
                          '--polymarket', self.env['CATALOG_MARKET']], units[unit][-7:])
        early = 'ai-football-profitability-reference-prematch-benfica'
        for name in (early, f'{early}-catalog'):
            self.assertIn('--on-calendar=2026-11-19 17:30:00 UTC', units[name])
            self.assertIn('--before', units[name])
        self.assertIn('2026-11-19T17:30:00Z', units['ai-football-profitability-reference-replay-benfica'])
        result, units = self.invoke('2026-11-19T13:00:00+00:00', active=f'{unit}.timer')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn(unit, units)
        self.assertIn(f'{early}-catalog', units)
        result, units = self.invoke('2026-11-19T19:15:00+00:00', active=f'{early}.service')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn(early, units)
        self.assertIn('--on-calendar=2026-11-19 19:15:05 UTC', units[f'{early}-catalog'])
        result, units = self.invoke('2026-11-19T19:57:00+00:00')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn(early, units)
        self.assertNotIn(f'{early}-catalog', units)

    def test_prematch_capture_starts_only_inside_its_bounded_window(self):
        begin, end = '2026-11-19T17:30:00Z', '2026-11-19T19:57:00Z'
        label = 'uwcl-slb-asr-2026-11-19'
        args = ('--capture', begin, '--before', end, label, '--polymarket', f'{label}-slb')
        result, _ = self.invoke('2026-11-19T19:30:00+00:00', *args)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([label, '--samples', '41', '--interval-seconds', '40',
                          '--end-at', str(int(datetime.fromisoformat(end).timestamp())), *args[-2:]],
                         result.stdout.splitlines())
        for outside in ('2026-11-19T17:29:59+00:00', '2026-11-19T19:57:00+00:00'):
            result, _ = self.invoke(outside, *args)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn('--samples', result.stdout)
        result, _ = self.invoke(begin, *args, '--polymarket-event', f'{label}-more-markets')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([label, '--samples', '883', '--interval-seconds', '10'], result.stdout.splitlines()[:5])

    def test_reviewed_catalog_is_scheduled_ahead_of_kickoff(self):
        label = 'bra-vit-bra-2026-11-19'
        unit = f'ai-football-profitability-reference-{label}'
        (self.archive / f'catalog-{label}.json').write_text('{}\n')
        self.env['CATALOG_MARKET'] = f'{label}-total-3pt5'
        args = ('--schedule-catalog', label, 'leyu:5824084', '2026-11-19T22:30:00Z')
        result, entries = self.invoke('2026-11-19T19:40:00Z', *args)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({f'{unit}-{suffix}' for suffix in ('prematch', 'live', 'replay')}, entries.keys())
        self.assertIn('--on-calendar=2026-11-19 20:00:00 UTC', entries[f'{unit}-prematch'])
        self.assertIn('--on-calendar=2026-11-19 22:27:00 UTC', entries[f'{unit}-live'])
        self.assertIn('--on-calendar=2026-11-20 01:02:00 UTC', entries[f'{unit}-replay'])
        self.assertEqual([label, 'leyu:5824084', '2026-11-19T20:00:00Z', '2026-11-20T01:02:00Z',
                          self.bindings.name], entries[f'{unit}-replay'][-5:])
        result, entries = self.invoke('2026-11-19T20:10:00Z', *args, active=f'{unit}-prematch.service',
                                      state='activating')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn(f'{unit}-prematch', entries)
        result, entries = self.invoke('2026-11-20T01:10:00Z', *args)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(entries)
        result, entries = self.invoke('2026-11-19T19:40:00Z', *args[:2], 'not-leyu', args[3])
        self.assertEqual(2, result.returncode)
        self.assertFalse(entries)

    def test_expanded_capture_uses_five_seconds_and_an_absolute_deadline(self):
        begin = '2026-11-19T19:57:00Z'
        label = 'uwcl-slb-asr-2026-11-19'
        extra = ['--polymarket-event', f'{label}-more-markets', '--polymarket', f'{label}-total-3pt5']
        call = ('--capture', begin, f'{label}-catalog', *extra)
        result, _ = self.invoke('2026-11-19T19:58:00+00:00', *call)
        self.assertEqual(0, result.returncode, result.stderr)
        end = int(datetime.fromisoformat('2026-11-19T22:07:21Z').timestamp())
        self.assertEqual([f'{label}-catalog', '--samples', '1553', '--interval-seconds', '5',
                          '--end-at', str(end), *extra], result.stdout.splitlines())
        after, _ = self.invoke('2026-11-19T22:07:22+00:00', *call)
        self.assertNotIn('--samples', after.stdout)

    def test_interrupted_stream_resumes_until_fixed_end_and_running_stream_is_preserved(self):
        now = '2026-11-19T12:09:00+00:00'
        result, units = self.invoke(now)
        self.assertEqual(0, result.returncode, result.stderr)
        stream = units[SVAY_STREAM]
        self.assertIn('--on-calendar=2026-11-19 12:09:05 UTC', stream)
        self.assertEqual(str(int(datetime.fromisoformat('2026-11-19T12:15:00+00:00').timestamp())),
                         stream[stream.index('--end-at') + 1])
        result, units = self.invoke(now, active=f'{SVAY_STREAM}.service', state='activating')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn(SVAY_STREAM, units)
        result, units = self.invoke('2026-11-19T12:15:00+00:00')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn(SVAY_STREAM, units)

    def test_elapsed_windows_replay_only_when_usable_archive_exists(self):
        capture = self.archive / f'{SVAY_LABEL}-2026-11-19T11:57:00Z.jsonl'
        capture.touch()
        now = '2026-11-20T06:00:00+00:00'
        result, units = self.invoke(now)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(units)
        capture.write_text('{}\n')
        (self.archive / f'{SVAY_LABEL}-preview-2026-11-19T12:36:00Z.json').write_text('{}\n')
        result, units = self.invoke(now)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({SVAY_REPLAY}, set(units))
        self.assertIn('--on-calendar=2026-11-20 06:00:05 UTC', units[SVAY_REPLAY])
        self.assertEqual(
            [SVAY_LABEL, 'leyu:5860873', '2026-11-19T11:57:00Z', '2026-11-19T14:32:00Z', self.bindings.name],
            units[SVAY_REPLAY][-5:],
        )
        (self.archive / f'{SVAY_LABEL}-replay-2026-11-20T06:00:05Z.json').write_text('{}\n')
        result, units = self.invoke(now)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(units)

    def test_mls_score_windows_keep_the_verified_schedule(self):
        result, units = self.invoke('2026-11-19T23:30:00Z')
        self.assertEqual(0, result.returncode, result.stderr)
        orlando = units['ai-football-profitability-reference-score-orlando']
        seattle = units['ai-football-profitability-reference-score-seattle']
        self.assertIn('--on-calendar=2026-11-20 00:27:00 UTC', orlando)
        self.assertIn('--on-calendar=2026-11-20 02:57:00 UTC', seattle)
        self.assertEqual(['--kalshi-milestone', '57142069-94b7-53cb-ba17-d2165114ac34'], orlando[-2:])

    def test_missing_bindings_prevents_registration(self):
        self.bindings.unlink()
        result, units = self.invoke('2026-11-19T11:00:00+00:00')
        self.assertEqual(2, result.returncode)
        self.assertIn('bindings', result.stderr)
        self.assertFalse(units)
