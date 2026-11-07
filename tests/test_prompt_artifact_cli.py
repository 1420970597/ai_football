import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from infra.remote.deploy_prompt_artifact import verified_report
from infra.remote import deploy_prompt_artifact as release_cli


class PromptArtifactCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.app = self.root / 'image'
        self.hashes = {}
        for name in ('app/code.py', 'migrations/001.sql', 'requirements.txt'):
            path = self.app / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
            self.hashes['services/api/' + name] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.report = {'source_parent_commit': 'a' * 40, 'committed_source_verified': True,
            'tests': 1, 'failures': 0, 'errors': 0, 'skipped': 0, 'source_sha256': self.hashes}
        self.path = self.root / 'backend-tests.json'

    def verify(self, report=None):
        self.path.write_text(json.dumps(report or self.report))
        return verified_report(self.path, 'a' * 40, self.app)

    def test_exact_image_files_and_successful_frozen_report_are_required(self):
        raw, report = self.verify()
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.report, report)
        for key, bad in [('source_parent_commit', 'b' * 40), ('committed_source_verified', False),
                         ('tests', True), ('failures', 1), ('errors', 1), ('skipped', 1)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.verify({**self.report, key: bad})

    def test_modified_or_additional_code_and_missing_hashes_are_rejected(self):
        for mode in ('modified', 'extra', 'hashes'):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                if mode == 'modified':
                    (self.app / 'app/code.py').write_text('changed')
                    self.verify()
                elif mode == 'extra':
                    (self.app / 'app/code.py').write_bytes(b'app/code.py')
                    (self.app / 'app/extra.py').write_text('extra')
                    self.verify()
                else:
                    (self.app / 'app/extra.py').unlink()
                    self.verify({**self.report, 'source_sha256': None})

    def test_requirements_drift_is_rejected(self):
        (self.app / 'requirements.txt').write_text('different dependency')
        with self.assertRaises(ValueError):
            self.verify()


class PromptArtifactCliRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_only_configuration_is_rejected_before_database_connection(self):
        environment = {'DATABASE_URL': 'postgresql://isolated/test', 'HERMES_API_KEY': 'isolated-worker'}
        args = SimpleNamespace(report='verified-separately.json', source_commit='a' * 40)
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(release_cli, 'verified_report', return_value=(b'isolated report', {'tests': 1})), \
                patch.object(release_cli.asyncpg, 'connect', AsyncMock()) as connect:
            with self.assertRaisesRegex(RuntimeError, 'APP_SESSION_SECRET'):
                await release_cli.run(args)
        connect.assert_not_awaited()
