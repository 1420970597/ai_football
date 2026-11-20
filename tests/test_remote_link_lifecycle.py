"""Fixed SSH scopes and actual subprocess cleanup, without remote connections."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from infra.remote.ssh_link import commands, health, run, REMOTE_SOCKET

CONFIG = {'ssh_config': '/run/storage/ssh_config', 'ssh_host': 'storage-vps'}


class RemoteLinkLifecycleTests(unittest.TestCase):
    def test_split_is_explicit_and_keeps_exactly_the_original_targets(self):
        self.assertEqual(1, len(commands(CONFIG)))
        self.assertEqual(commands(CONFIG), commands({**CONFIG, 'split_connections': False}))
        api, control = commands({**CONFIG, 'split_connections': True})
        self.assertIn(REMOTE_SOCKET + ':api:8080', api)
        self.assertNotIn('-L', api)
        self.assertNotIn('-R', control)
        self.assertIn('0.0.0.0:18092:127.0.0.1:18092', control)
        for command in (api, control):
            self.assertIn('ControlMaster=no', command)
            self.assertIn('ControlPath=none', command)
            self.assertIn('StrictHostKeyChecking=yes', command)
            self.assertIn('ExitOnForwardFailure=yes', command)
        for invalid in (None, 0, 1, 'true', [], {}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                commands({**CONFIG, 'split_connections': invalid})

    def test_either_child_failure_reaps_both_and_removes_state(self):
        for failed in (0, 1):
            children = []
            real_spawn = subprocess.Popen
            def spawn(*args, **kwargs):
                process = real_spawn(*args, **kwargs)
                children.append(process)
                return process
            planned = [[sys.executable, '-c', 'import time;time.sleep(60)'] for _ in range(2)]
            planned[failed] = [sys.executable, '-c', 'raise SystemExit(3)']
            with tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / 'state.json'
                with patch('infra.remote.ssh_link.commands', return_value=planned), \
                        patch('infra.remote.ssh_link.subprocess.Popen', side_effect=spawn):
                    self.assertEqual(1, run(CONFIG, state_path=state))
                self.assertEqual(2, len(children))
                self.assertTrue(all(child.poll() is not None for child in children))
                self.assertFalse(state.exists())
                self.assertFalse(state.with_suffix('.pending').exists())

    def test_partial_spawn_failure_reaps_first_child(self):
        children = []
        real_spawn = subprocess.Popen
        def spawn(*args, **kwargs):
            if children:
                raise OSError('synthetic second spawn failure')
            process = real_spawn([sys.executable, '-c', 'import time;time.sleep(60)'], **kwargs)
            children.append(process)
            return process
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            with patch('infra.remote.ssh_link.subprocess.Popen', side_effect=spawn):
                with self.assertRaises(OSError):
                    run({**CONFIG, 'split_connections': True}, state_path=state)
            self.assertIsNotNone(children[0].poll())
            self.assertFalse(state.exists())
            self.assertFalse(state.with_suffix('.pending').exists())

    def test_split_health_rejects_dead_or_invalid_secondary_pid_before_http(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            for pids in ([os.getpid(), 2147483647], [os.getpid(), True], [],
                         [os.getpid(), os.getpid()], 'not-a-list'):
                state.write_text(json.dumps({'pid': os.getpid(), 'pids': pids}))
                with patch('infra.remote.ssh_link.http.client.HTTPConnection') as connection:
                    self.assertEqual(1, health(state_path=state, token='synthetic'))
                    connection.assert_not_called()

    def test_sigterm_reaps_real_children_and_removes_private_atomic_state(self):
        program = '''import sys
from pathlib import Path
from infra.remote import ssh_link
ssh_link.commands=lambda config:[[sys.executable,'-c','import time;time.sleep(60)'] for _ in range(2)]
raise SystemExit(ssh_link.run({},state_path=Path(sys.argv[1])))
'''
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            supervisor = subprocess.Popen([sys.executable, '-c', program, str(state)],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not state.exists() and supervisor.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(state.exists())
                children = json.loads(state.read_text())['pids']
                self.assertEqual(2, len(children))
                self.assertEqual(0o600, state.stat().st_mode & 0o777)
                self.assertFalse(state.with_suffix('.pending').exists())
                supervisor.terminate()
                self.assertEqual(0, supervisor.wait(timeout=8))
                self.assertFalse(state.exists())
                for pid in children:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
            finally:
                if supervisor.poll() is None:
                    supervisor.kill()
                supervisor.communicate(timeout=8)
