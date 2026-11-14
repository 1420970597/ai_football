import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock,patch
from infra.monitor.idle_deploy import deploy

class IdleDeployTests(unittest.TestCase):
    def scenario(self,deploy_exit=0,deploy_error=None,gate_code=0):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);config=root/'private.json';script=root/'gate.py'
            config.write_text('{}');script.write_text('# gate')
            gate=MagicMock();gate.stdin=io.BytesIO();gate.stdout=io.BytesIO(b'{"event":"idle_gate_held"}\n');gate.stderr=io.BytesIO();gate.wait.return_value=gate_code
            calls=[]
            def run(args,**kwargs):
                calls.append(args)
                if args[:2]==['docker','compose']:
                    if deploy_error:raise deploy_error
                    return subprocess.CompletedProcess(args,deploy_exit)
                return subprocess.CompletedProcess(args,0)
            with patch('infra.monitor.idle_deploy.subprocess.Popen',return_value=gate),patch('infra.monitor.idle_deploy.subprocess.run',side_effect=run),patch('infra.monitor.idle_deploy.select.select',return_value=([gate.stdout],[],[])):
                result=None;error=None
                try:result=deploy(config,script,Path('/tmp/tg-idle-unit-test'),['api','local-mcp-api'],root/'log')
                except Exception as exc:error=exc
            self.assertEqual(['docker','compose'],calls[0][:2])
            self.assertEqual(['docker','exec','ai-football-v2-review-worker-1','touch','/tmp/tg-idle-unit-test'],calls[-1])
            self.assertTrue(gate.stdout.closed)
            return result,error

    def test_success_deploys_and_releases_in_same_call(self):
        result,error=self.scenario()
        self.assertIsNone(error)
        self.assertEqual(0,result['gate_exit_code'])
        self.assertLess(result['held_seconds'],1)

    def test_failed_deploy_keeps_failure_and_releases(self):
        result,error=self.scenario(deploy_exit=1)
        self.assertIsNone(result)
        self.assertEqual('candidate_deploy_failed',str(error))

    def test_timeout_also_releases_and_propagates(self):
        _,error=self.scenario(deploy_error=subprocess.TimeoutExpired('docker',12))
        self.assertIsInstance(error,subprocess.TimeoutExpired)

    def test_gate_failure_is_not_success(self):
        _,error=self.scenario(gate_code=1)
        self.assertEqual('gate_handoff_failed',str(error))

    def test_other_services_never_start_a_gate(self):
        with patch('infra.monitor.idle_deploy.subprocess.Popen')as start:
            with self.assertRaises(ValueError):deploy(Path('/x'),Path('/y'),Path('/tmp/tg-idle-unit'),['collector'],Path('/z'))
            start.assert_not_called()
