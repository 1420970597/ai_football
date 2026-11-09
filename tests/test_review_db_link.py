import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import yaml

from infra.remote import review_link


class ReviewLinkTests(unittest.TestCase):
    def test_fixed_forward_and_host_key_are_not_shared_or_configurable(self):
        args = review_link.command({'ssh_config': '/run/storage/ssh_config', 'ssh_host': 'project-vps'})
        self.assertEqual('0.0.0.0:18094:127.0.0.1:18094', args[args.index('-L')+1])
        self.assertIn('StrictHostKeyChecking=yes', args)
        self.assertIn('ExitOnForwardFailure=yes', args)
        self.assertIn('ControlMaster=no', args)
        self.assertIn('ControlPath=none', args)
        self.assertNotIn('-R', args)
        for updates in ({'port': 22}, {'ssh_host': '-oProxyCommand=bad'}, {'ssh_config': '/tmp/config'},
                        {'ssh_host': 'host\nextra'}, {'ssh_host': True}, {'ssh_host': 'other:22'}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                review_link.command({'ssh_config': '/run/storage/ssh_config', 'ssh_host': 'project-vps', **updates})

    def test_health_requires_live_mcp_readiness_and_bounds_response(self):
        for code, body, expected in (
                (200, {'status': 'ready', 'role': 'mcp'}, 0), (200, {'status': 'ok'}, 1),
                (503, {'status': 'ready', 'role': 'mcp'}, 1), (200, ['ready'], 1),
                (200, 'x'*4100, 1), (200, 'invalid-json', 1)):
            connection = Mock()
            response = connection.getresponse.return_value
            response.status = code
            response.read.return_value = json.dumps(body).encode() if isinstance(body, (dict, list)) else body.encode()
            with self.subTest(body=str(body)[:40]), patch.object(review_link.http.client, 'HTTPConnection', return_value=connection) as make:
                self.assertEqual(expected, review_link.health())
            make.assert_called_once_with('127.0.0.1', 18094, timeout=2)
            connection.request.assert_called_once_with('GET', '/readyz')
            response.read.assert_called_once_with(4097)
            connection.close.assert_called_once_with()

    def test_disconnection_is_unhealthy_and_connection_closes(self):
        connection = Mock()
        connection.request.side_effect = OSError('must not be emitted')
        with patch.object(review_link.http.client, 'HTTPConnection', return_value=connection):
            self.assertEqual(1, review_link.health())
        connection.close.assert_called_once_with()

    def test_exec_replaces_process_and_does_not_echo_configuration_failures(self):
        with patch.object(review_link.sys, 'argv', ['review_link.py']), \
                patch.object(review_link.Path, 'read_text', return_value=json.dumps(
                    {'ssh_config': '/run/storage/ssh_config', 'ssh_host': 'project-vps'})), \
                patch.object(review_link.os, 'execvp', side_effect=OSError('private-target-must-not-log')) as execute, \
                patch('builtins.print') as logged:
            self.assertEqual(1, review_link.main())
        self.assertEqual('ssh', execute.call_args.args[0])
        logged.assert_called_once_with('review_link_unavailable', flush=True)

    def test_compose_only_adds_restricted_remote_process_and_private_local_forward(self):
        root = Path(__file__).resolve().parents[1]
        remote = yaml.safe_load((root/'infra/remote/compose.review-tools.remote.yml').read_text())
        self.assertEqual({'review-tools-api'}, set(remote['services']))
        service = remote['services']['review-tools-api']
        self.assertIn('127.0.0.1', service['command'])
        self.assertIn('app.mcp_app:app', service['command'])
        self.assertEqual('host', service['network_mode'])
        self.assertEqual(1, len(service['env_file']))
        self.assertNotIn('volumes', service)
        self.assertNotIn('environment', service)
        self.assertEqual(.2, service['cpus'])
        local = yaml.safe_load((root/'infra/remote/compose.review-tools.local.yml').read_text())
        self.assertEqual({'hermes-mcp', 'review-db-link'}, set(local['services']))
        link = local['services']['review-db-link']
        self.assertNotIn('ports', link)
        self.assertNotIn('environment', link)
        self.assertTrue(all(v['read_only'] for v in link['volumes']))
        self.assertNotIn('tunnel.json', str(link))
        self.assertEqual(.05, link['cpus'])
