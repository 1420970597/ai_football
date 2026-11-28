from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from infra.android.release_policy import REQUIRED_SCOPES, release_evidence, validate_identity, verify_package


class AndroidReleasePolicyTests(unittest.TestCase):
    def package(self):
        return ("package: name='com.aifootball.platform' versionCode='20001' versionName='2.0.0-rc.1'\n"
                "uses-permission: name='android.permission.INTERNET'\n"
                "uses-permission: name='android.permission.ACCESS_NETWORK_STATE'\n"
                "Signer #1 certificate DN: CN=Explicit ephemeral test signer\n"
                "Signer #1 certificate SHA-256 digest: " + "a" * 64 + "\n"
                "Verified using v2 scheme (APK Signature Scheme v2): true\n")

    def test_identity_rejects_development_mutable_and_invalid_versions(self):
        validate_identity('2.0.0-rc.1', 20001, 'b' * 40)
        for version, code, commit in [('2.0.0-dev', 20001, 'b'*40), ('02.0.0', 1, 'b'*40),
                                      ('2.0.0', 0, 'b'*40), ('2.0.0', 1, 'HEAD')]:
            with self.subTest(version=version, code=code):
                with self.assertRaises(ValueError):
                    validate_identity(version, code, commit)

    def test_package_accepts_only_reviewed_identity_permissions_and_signer(self):
        package = self.package()
        self.assertEqual('a'*64, verify_package(package, '2.0.0-rc.1', 20001, 'A'*64))
        for changed in [package.replace("versionCode='20001'", "versionCode='20000'"),
                        package.replace('com.aifootball.platform', 'other.app'),
                        package.replace('Explicit ephemeral test signer', 'Android Debug'),
                        package + "uses-permission: name='android.permission.CAMERA'\n",
                        package + "uses-permission: name='android.permission.INTERNET'\n",
                        package.replace("uses-permission: name='android.permission.INTERNET'\n", ''),
                        package + "uses-permission-sdk-23: name='android.permission.CAMERA'\n",
                        package + 'application-debuggable\n', package.replace('true', 'false')]:
            with self.subTest(changed=changed[-60:]):
                with self.assertRaises(ValueError):
                    verify_package(changed, '2.0.0-rc.1', 20001, 'a'*64)
        with self.assertRaises(ValueError):
            verify_package(package, '2.0.0-rc.1', 20001, 'b'*64)

    def fixture(self, root):
        evidence = root / 'evidence'
        evidence.mkdir()
        artifact = root / 'result.log'
        artifact.write_text('explicit synthetic fixture; not production evidence')
        for scope in REQUIRED_SCOPES:
            record = {'scope': scope, 'status': 'passed', 'exit_code': 0,
                      'source_commit': 'b'*40, 'version': '2.0.0', 'real_environment': True,
                      'checked_at': datetime.now(timezone.utc).isoformat(), 'command': 'explicit fixture',
                      'apk_sha256': 'd'*64,
                      'artifacts': [{'path': 'result.log', 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}]}
            (evidence / f'{scope}.json').write_text(json.dumps(record))
        return evidence

    def test_stable_gate_requires_every_current_source_bound_real_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self.fixture(root)
            self.assertEqual(len(REQUIRED_SCOPES), len(release_evidence(root, evidence, 'b'*40, '2.0.0', 'd'*64)))
            file = evidence / 'android_device.json'
            original = json.loads(file.read_text())
            for patch in [{'source_commit': 'c'*40}, {'real_environment': False}, {'exit_code': 1},
                          {'checked_at': '2020-01-01T00:00:00Z'}, {'artifacts': []}, {'command': ''}]:
                file.write_text(json.dumps({**original, **patch}))
                with self.subTest(patch=patch):
                    with self.assertRaises(ValueError):
                        release_evidence(root, evidence, 'b'*40, '2.0.0', 'd'*64)
            file.unlink()
            with self.assertRaisesRegex(ValueError, 'android_device'):
                release_evidence(root, evidence, 'b'*40, '2.0.0', 'd'*64)

    def test_stable_gate_rejects_artifact_tampering_and_prerelease(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self.fixture(root)
            (root / 'result.log').write_text('tampered')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                release_evidence(root, evidence, 'b'*40, '2.0.0', 'd'*64)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self.fixture(root)
            for file in evidence.glob('*.json'):
                record = json.loads(file.read_text())
                file.write_text(json.dumps({**record, 'version': '2.0.0-rc.1'}))
            with self.assertRaisesRegex(ValueError, 'prerelease'):
                release_evidence(root, evidence, 'b'*40, '2.0.0-rc.1', 'd'*64)

    def test_stable_gate_rejects_outside_artifacts_symlink_records_and_naive_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self.fixture(root)
            file = evidence / 'android_device.json'
            record = json.loads(file.read_text())
            file.write_text(json.dumps({**record, 'checked_at': '2026-10-27T00:00:00'}))
            with self.assertRaisesRegex(ValueError, 'timestamp'):
                release_evidence(root, evidence, 'b'*40, '2.0.0', 'd'*64)
            file.write_text(json.dumps({**record, 'artifacts': [{'path': '../outside', 'sha256': 'a'*64}]}))
            with self.assertRaisesRegex(ValueError, 'project-local'):
                release_evidence(root, evidence, 'b'*40, '2.0.0', 'd'*64)
            file.unlink()
            file.symlink_to(evidence / 'statistics.json')
            with self.assertRaisesRegex(ValueError, 'symbolic'):
                release_evidence(root, evidence, 'b'*40, '2.0.0', 'd'*64)

    def test_device_evidence_must_match_the_exact_signed_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self.fixture(root)
            with self.assertRaisesRegex(ValueError, 'exact signed APK'):
                release_evidence(root, evidence, 'b'*40, '2.0.0', 'e'*64)


if __name__ == '__main__':
    unittest.main()
