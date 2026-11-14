import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

path=Path(__file__).parents[1]/'infra/monitor'
sys.path.insert(0,str(path))
from audit_runner import review_message


class AuditRunnerTests(unittest.TestCase):
    def result(self,**extra):
        value=dict(classification='uncertain',verdict='uncertain',tool_count=0,persistent_session=False)
        value.update(extra)
        return SimpleNamespace(returncode=0,stdout=json.dumps(value).encode())

    def test_uncertainty_is_preserved_without_retrying_for_a_pass(self):
        with patch('audit_runner.subprocess.run',side_effect=[
                SimpleNamespace(returncode=0,stdout=b'private-image'),self.result()])as run:
            result=review_message(path.parents[1],{'raw_text':'private-source'})
        self.assertEqual('uncertain',result['verdict'])
        self.assertEqual(2,run.call_count)
        self.assertIn('hermes',run.call_args.args[0])
        self.assertEqual(['nice','-n','19'], run.call_args.args[0][6:9])
        self.assertEqual(['timeout','--signal=TERM','--kill-after=2','60'], run.call_args.args[0][9:13])
        self.assertGreater(run.call_args.kwargs['timeout'],62)
        self.assertNotIn('private-source',json.dumps(result))

    def test_transport_or_permission_failure_cannot_be_sealed_as_valid_audit(self):
        for response in [SimpleNamespace(returncode=1,stdout=b''),
                         SimpleNamespace(returncode=124,stdout=b''),self.result(tool_count=1),
                         self.result(persistent_session=True),self.result(verdict='guess')]:
            with patch('audit_runner.subprocess.run',side_effect=[
                    SimpleNamespace(returncode=0,stdout=b'input'),response]):
                with self.assertRaises((RuntimeError,ValueError)):
                    review_message(path.parents[1],{})
