import os
import unittest
from unittest.mock import patch
from infra.monitor.audit_runner import review_command


class AuditResourceIsolationTests(unittest.TestCase):
    def test_default_audit_uses_its_own_hermes_container(self):
        with patch.dict(os.environ,{},clear=True):
            command=review_command()
        self.assertEqual(['docker','exec','-i','--user','hermes',
            'ai-football-tg-semantic-hermes','nice','-n','19','python','-c'],command)

    def test_realtime_container_cannot_be_configured_as_audit_target(self):
        with self.assertRaisesRegex(ValueError,'must_not_share'):
            review_command('ai-football-v2-hermes-1')

    def test_explicit_private_container_is_preserved(self):
        with patch.dict(os.environ,{'EXPERT_AUDIT_HERMES_CONTAINER':'project-audit.2'}):
            self.assertEqual('project-audit.2',review_command()[5])

    def test_empty_whitespace_and_shell_arguments_are_rejected(self):
        for value in (' ','--privileged','audit;stop','audit/name'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                review_command(value)
