import json
from pathlib import Path
import tempfile
import unittest

from infra.release.manifest import COMPONENT_SERVICES, compose_override, make_manifest


class ReleaseManifestTests(unittest.TestCase):
    def fixture(self, root):
        (root / "infra/release").mkdir(parents=True)
        (root / "infra/release/images.json").write_text(json.dumps(dict.fromkeys(COMPONENT_SERVICES, "explicit test recipe")))
        artifacts = root / "artifacts"
        artifacts.mkdir()
        for kind in ("debug", "unsigned"):
            (artifacts / f"ballnews-2.0.0-dev-{kind}.apk").write_bytes(b"explicit test artifact")
        (artifacts / "source-parent-commit.txt").write_text("a" * 40)
        (artifacts / "source-worktree-status.txt").write_text("")
        (artifacts / 'package-verification.txt').write_text(
            "package: name='com.aifootball.platform' versionCode='20000' versionName='2.0.0-dev'\n"
            "uses-permission: name='android.permission.INTERNET'\n"
            "uses-permission: name='android.permission.ACCESS_NETWORK_STATE'\n"
            "CN=Android Debug\nVerified using v2 scheme (APK Signature Scheme v2): true")
        for name in ('config.yaml', 'AGENTS.md', 'runner.py', 'skills/expert-review/SKILL.md'):
            path = root / 'integrations/hermes' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('explicit configuration fixture')
        return artifacts, {name: f"ghcr.io/example/{name}@sha256:" + "b" * 64 for name in COMPONENT_SERVICES}

    def test_development_manifest_preserves_source_digest_and_signing_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts, images = self.fixture(root)
            result = make_manifest(root, "a" * 40, images, artifacts)
            self.assertFalse(result["production_ready"])
            self.assertEqual(images, result["images"])
            self.assertEqual("unsigned", result["assets"]["ballnews-2.0.0-dev-unsigned.apk"]["signing"])
            self.assertEqual('2.0.0-dev-'+'a'*40,result['compose_image_tag'])

    def test_mutable_image_or_dirty_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts, images = self.fixture(root)
            with self.assertRaises(ValueError):
                make_manifest(root, "a" * 40, {**images,"api": "ghcr.io/example/api:latest"}, artifacts)
            (artifacts / "source-worktree-status.txt").write_text(" M android/source")
            with self.assertRaises(ValueError):
                make_manifest(root, "a" * 40, images, artifacts)
            (artifacts / "source-worktree-status.txt").write_text("")
            with self.assertRaises(ValueError):
                make_manifest(root, "c" * 40, images, artifacts)

    def test_mismatched_package_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts, images = self.fixture(root)
            file = artifacts / 'package-verification.txt'
            file.write_text(file.read_text().replace('2.0.0-dev', '1.0.0'))
            with self.assertRaises(ValueError):
                make_manifest(root, 'a' * 40, images, artifacts)

    def test_package_identity_and_permission_policy_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts, images = self.fixture(root)
            package = artifacts / 'package-verification.txt'
            package.write_text(package.read_text().replace("com.aifootball.platform", "com.example.wrong"))
            with self.assertRaises(ValueError):
                make_manifest(root, 'a' * 40, images, artifacts)
            package.write_text(package.read_text().replace("com.example.wrong", "com.aifootball.platform") +
                               "\nuses-permission: name='android.permission.READ_PHONE_STATE'")
            with self.assertRaises(ValueError):
                make_manifest(root, 'a' * 40, images, artifacts)

    def test_compose_override_pins_all_services_and_does_not_copy_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            artifacts,images=self.fixture(root)
            manifest=make_manifest(root,'a'*40,images,artifacts)
            manifest['environment']={'DO_NOT_COPY':'explicit fixture'}
            document=compose_override(manifest)
            self.assertEqual({'api','migrate','review-worker','collector','executor','hermes-mcp','web','storage-tunnel','hermes'},set(document['services']))
            for name in ('api','migrate','review-worker'):
                self.assertEqual({'image':images['api']},document['services'][name])
            self.assertEqual({'image':manifest['hermes']},document['services']['hermes'])
            self.assertNotIn('DO_NOT_COPY',json.dumps(document))
            self.assertIs(True,document['x-development-only'])

    def test_compose_override_rejects_partial_mutable_or_mislabelled_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            artifacts,images=self.fixture(root)
            manifest=make_manifest(root,'a'*40,images,artifacts)
            mutations=[{'images':{'api':images['api']}},
                       {'images':dict(images,web='ghcr.io/example/web:latest')},
                       {'channel':'production'},{'production_ready':True},
                       {'source_commit':'not-a-commit'},{'hermes':'nousresearch/hermes-agent:latest'}]
            for mutation in mutations:
                with self.subTest(mutation=next(iter(mutation))):
                    with self.assertRaises(ValueError):
                        compose_override({**manifest,**mutation})
