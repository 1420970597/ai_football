import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from infra.config.check_compose import validate
from infra.config.prepare_runtime import prepare_runtime, RuntimePreparationError
from infra.config.read_only_audit import _check_runtime_env
from infra.config.storage_probe import object_probe, safe_prefix, storage_acceptance_gate
from tests import test_config_reuse


class RuntimePreparationTests(unittest.TestCase):
    def test_preparation_is_idempotent_and_does_not_create_or_replace_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = prepare_runtime(root, apply=True)
            self.assertEqual(first, prepare_runtime(root, apply=True))
            self.assertTrue(all(item["private"] for item in first["directories"]))
            self.assertTrue(all(item["state"] == "unconfigured" for item in first["references"].values()))
            self.assertFalse(first["secrets_created"])
            secret = root / ".runtime/leyu-app-signing.json"
            secret.write_text("private-value-must-not-appear")
            secret.chmod(0o600)
            report = prepare_runtime(root, apply=True)
            self.assertEqual("private-value-must-not-appear", secret.read_text())
            self.assertEqual(0o600, secret.stat().st_mode & 0o777)
            self.assertNotIn("private-value", json.dumps(report))
            self.assertFalse(report["references"]["collector_signing"]["readable_by_service"])
            self.assertEqual("permission_review_required", report["references"]["collector_signing"]["state"])
            ca = root / ".runtime/s3-ca.crt"
            ca.write_text("public-ca")
            ca.chmod(0o644)
            self.assertEqual("ready", prepare_runtime(root)["references"]["s3_ca"]["state"])

    def test_symlink_and_file_conflict_refused_before_any_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".runtime").symlink_to(root / "other")
            with self.assertRaises(RuntimePreparationError):
                prepare_runtime(root, apply=True)
            self.assertFalse((root / "other").exists())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".runtime").write_text("keep")
            with self.assertRaises(RuntimePreparationError):
                prepare_runtime(root, apply=True)
            self.assertEqual("keep", (root / ".runtime").read_text())

    def test_config_audit_does_not_follow_external_secret_references(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime.env"
            runtime.write_text("LEYU_SIGNING_FILE=../outside-signing.json\n")
            result = _check_runtime_env(runtime, root)
            self.assertIn({"key": "LEYU_SIGNING_FILE", "reason": "referenced_path_outside_project"}, result["invalid"])

    def test_restricted_signing_group_requires_explicit_owner_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            prepare_runtime(root,apply=True)
            for name in ("leyu-app-signing.json","leyu-app-discovery.json"):
                (root/".runtime"/name).write_text("private-content")
            with self.assertRaises(RuntimePreparationError):
                prepare_runtime(root,signing_group=True)
            with patch("infra.config.prepare_runtime.os.geteuid",return_value=0), patch("infra.config.prepare_runtime.os.chown") as change:
                result=prepare_runtime(root,apply=True,signing_group=True)
            self.assertEqual(2,change.call_count)
            self.assertTrue(result["signing_group_applied"])
            self.assertTrue(all((root/".runtime"/name).stat().st_mode & 0o777==0o440 for name in ("leyu-app-signing.json","leyu-app-discovery.json")))


class ComposeResourceGateTests(unittest.TestCase):
    def test_api_can_authenticate_internal_hermes_config_but_never_get_provider_key(self):
        document=test_config_reuse.ComposeBoundaryTests().fixture()
        document["services"]["api"]["environment"]["HERMES_API_KEY"]="fixture-internal-only"
        self.assertEqual([],validate(document,Path.cwd()))
        document["services"]["api"]["environment"]["HERMES_PROVIDER_API_KEY"]="fixture-provider"
        self.assertTrue(any("不应获取模型" in error for error in validate(document,Path.cwd())))
    def test_configs_cannot_escape_workspace_or_reference_unreviewed_external_resources(self):
        for config in ({"file": "/outside/signing.json"}, {"external": True}, {"file": "../escape.json"}):
            document = test_config_reuse.ComposeBoundaryTests().fixture()
            document["configs"] = {"signing": config}
            document["services"]["collector"]["configs"] = [{"source": "signing"}]
            document["services"]["collector"]["group_add"] = ["10005"]
            self.assertTrue(any("配置资源" in item for item in validate(document, Path.cwd())))
        document = test_config_reuse.ComposeBoundaryTests().fixture()
        document["configs"] = {"signing": {"file": ".runtime/signing.json"}}
        document["services"]["collector"]["configs"] = [{"source": "signing"}]
        document["services"]["collector"]["group_add"] = ["10005"]
        self.assertEqual([], validate(document, Path.cwd()))
        document["services"]["collector"]["cpus"] = "NaN"
        self.assertTrue(any("资源限额" in item for item in validate(document, Path.cwd())))
        document["services"]["api"]["group_add"]=["10005"]
        self.assertTrue(any("附加组" in item for item in validate(document,Path.cwd())))


class ReadOnlyStorageProbeTests(unittest.TestCase):
    def test_connection_success_does_not_hide_excessive_database_permissions(self):
        report={"database":{"state":"read_only_verified","least_privilege_verified":False},
                "objects":{"state":"read_only_verified"}}
        gate=storage_acceptance_gate(report)
        self.assertTrue(gate["read_only_connectivity_verified"])
        self.assertFalse(gate["runtime_least_privilege_verified"])
        self.assertIn("database_runtime_privileges_excessive_or_unknown",gate["blockers"])
        self.assertFalse(gate["production_restore_verified"])
    def test_prefix_budget_rejects_root_and_traversal(self):
        self.assertEqual("v2/", safe_prefix("v2/"))
        for value in ("", "/", "v2", "v2/../", "v2//"):
            with self.assertRaises(ValueError):
                safe_prefix(value)

    def test_probe_authenticates_only_bounded_prefix_and_performs_no_writes(self):
        client = MagicMock()
        client.list_objects_v2.return_value = {"Contents": [{"Key": "private-object"}]}
        environment = {"S3_ENDPOINT_URL": "https://storage.invalid", "S3_ACCESS_KEY": "private-access",
                       "S3_SECRET_KEY": "private-secret", "S3_BUCKET": "private-bucket", "S3_OBJECT_PREFIX": "v2/"}
        with patch("boto3.client", return_value=client):
            report = object_probe(environment)
        client.list_objects_v2.assert_called_once_with(Bucket="private-bucket", Prefix="v2/", MaxKeys=1)
        client.put_object.assert_not_called()
        client.delete_object.assert_not_called()
        client.close.assert_called_once()
        self.assertEqual("read_only_verified", report["state"])
        self.assertFalse(report["write_permission_verified"])
        self.assertNotIn("private-", json.dumps(report))
