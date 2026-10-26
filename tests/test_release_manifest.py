import json
from pathlib import Path
import tempfile
import unittest

from infra.release.manifest import make_manifest


class ReleaseManifestTests(unittest.TestCase):
    def fixture(self, root):
        (root / "infra/release").mkdir(parents=True)
        (root / "infra/release/images.json").write_text(json.dumps({"api": "ignored"}))
        artifacts = root / "artifacts"
        artifacts.mkdir()
        for kind in ("debug", "unsigned"):
            (artifacts / f"ballnews-2.0.0-dev-{kind}.apk").write_bytes(b"explicit test artifact")
        (artifacts / "source-parent-commit.txt").write_text("a" * 40)
        (artifacts / "source-worktree-status.txt").write_text("")
        (artifacts / 'package-verification.txt').write_text("versionName='2.0.0-dev'\nCN=Android Debug\nVerified using v2 scheme (APK Signature Scheme v2): true")
        for name in ('config.yaml', 'AGENTS.md', 'runner.py', 'skills/expert-review/SKILL.md'):
            path = root / 'integrations/hermes' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('explicit configuration fixture')
        return artifacts, {"api": "ghcr.io/example/api@sha256:" + "b" * 64}

    def test_development_manifest_preserves_source_digest_and_signing_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts, images = self.fixture(root)
            result = make_manifest(root, "a" * 40, images, artifacts)
            self.assertFalse(result["production_ready"])
            self.assertEqual(images, result["images"])
            self.assertEqual("unsigned", result["assets"]["ballnews-2.0.0-dev-unsigned.apk"]["signing"])

    def test_mutable_image_or_dirty_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts, images = self.fixture(root)
            with self.assertRaises(ValueError):
                make_manifest(root, "a" * 40, {"api": "ghcr.io/example/api:latest"}, artifacts)
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
