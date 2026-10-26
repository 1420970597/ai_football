import json
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest

from infra.config.read_only_audit import (
    _check_runtime_env,
    _summarize_env,
    archive_report,
    project_report,
)


class ReadOnlyConfigAuditTests(unittest.TestCase):
    def test_env_summary_contains_keys_but_never_values(self):
        result = _summarize_env(b"S3_SECRET_KEY='fixture-secret-value'\nEMPTY=\n")
        serialized = json.dumps(result)
        self.assertIn("S3_SECRET_KEY", serialized)
        self.assertNotIn("fixture-secret-value", serialized)
        self.assertEqual(["EMPTY"], result["empty_keys"])

    def test_archive_audit_reads_without_extracting(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "sample.tar"
            with tarfile.open(archive, "w") as output:
                info = tarfile.TarInfo("ai_football/.env")
                content = b"DATABASE_URL=postgresql://fixture\nS3_SECRET_KEY=fixture\n"
                info.size = len(content)
                info.mode = 0o600
                output.addfile(info, __import__("io").BytesIO(content))
                info = tarfile.TarInfo("ai_football/client/credentials.json")
                content = b'{"secret_key":"fixture"}'
                info.size = len(content)
                info.mode = 0o644
                output.addfile(info, __import__("io").BytesIO(content))
            result = archive_report(archive)
            self.assertEqual(2, result["member_count"])
            self.assertEqual(2, sum(item["found"] for item in result["samples"]))
            self.assertTrue(result["security_findings"])
            self.assertNotIn("fixture", json.dumps(result))

    def test_runtime_audit_separates_missing_invalid_and_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".runtime" / "runtime.env"
            runtime.parent.mkdir()
            runtime.write_text(
                "DATABASE_URL=postgresql://fixture\nDATABASE_TRANSPORT=ssh\n"
                "APP_SESSION_SECRET=fixture\nDATA_ENCRYPTION_KEY=fixture\n"
                "S3_ENDPOINT_URL=https://storage.invalid\nS3_ACCESS_KEY=fixture\n"
                "S3_SECRET_KEY=fixture\nS3_BUCKET=bucket\nS3_REGION=us-east-1\n"
                "S3_CA_FILE=.runtime/missing-ca.crt\n"
                "HERMES_MODEL=cline-pass/deepseek-v4.1-flash\n"
                "HERMES_PROVIDER_BASE_URL=https://model.invalid/v1\n"
                "HERMES_PROVIDER_API_KEY=fixture\n"
                "EXPERT_MCP_TOKEN=fixture\nCOLLECTOR_INTERNAL_TOKEN=fixture\n"
                "EXECUTOR_INTERNAL_TOKEN=fixture\nCOOKIE_SECURE=maybe\n",
                encoding="utf-8",
            )
            runtime.chmod(0o600)
            result = _check_runtime_env(runtime, root)
            self.assertTrue(result["private_mode"])
            self.assertIn("COOKIE_SECURE", {entry["key"] for entry in result["invalid"]})
            self.assertIn("S3_CA_FILE", {entry["key"] for entry in result["invalid"]})
            self.assertIn("S3_ENDPOINT_URL", result["external_unverified"])
            self.assertIn("HERMES_PROVIDER_BASE_URL", result["external_unverified"])
            self.assertNotIn("fixture", json.dumps(result))

    def test_s3_ca_certificate_may_be_readable_by_non_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".runtime" / "runtime.env"
            runtime.parent.mkdir()
            ca = runtime.parent / "s3-ca.crt"
            ca.write_text("fixture-public-ca\n", encoding="utf-8")
            ca.chmod(0o644)
            runtime.write_text("S3_CA_FILE=.runtime/s3-ca.crt\n", encoding="utf-8")
            runtime.chmod(0o600)
            result = _check_runtime_env(runtime, root)
            self.assertNotIn(
                {"key": "S3_CA_FILE", "reason": "referenced_secret_file_not_private"},
                result["invalid"],
            )
            self.assertEqual(
                {"key": "S3_CA_FILE", "present": True, "kind": "ca_certificate", "private_mode": False},
                result["referenced_files"][0],
            )
            self.assertNotIn("fixture-public-ca", json.dumps(result))

    def test_project_policy_does_not_claim_production_ready(self):
        result = project_report(Path.cwd())
        self.assertFalse(result["production_ready"])
        self.assertIn("external_reachability", result["not_verified"])
        self.assertNotIn(".env", result["source_policy"]["repository_secret_paths_outside_runtime"])
        self.assertNotIn(".env.example", result["source_policy"]["repository_secret_paths_outside_runtime"])


if __name__ == "__main__":
    unittest.main()
