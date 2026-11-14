from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch

import asyncpg

from infra.recovery.bundle import RecoveryError, validate_isolated_dsn
from infra.recovery.operations import cleanup_gate, recovery_plan
from infra.recovery.cli import IsolatedObjectDirectory, ROOT
from tests import test_recovery


class RecoveryBoundaryTests(unittest.TestCase):
    def test_query_options_cannot_redirect_an_isolated_database_url(self):
        base = "postgresql://recovery_test:isolated@ai-football-v2-pgtest/recovery_sample_test"
        validate_isolated_dsn(base)
        for suffix in ("?host=production", "?user=postgres", "?database=business", "?port=15432",
                       "?options=-csearch_path=business", "#ignored", "?sslmode=disable"):
            with self.subTest(suffix=suffix), self.assertRaises(RecoveryError):
                validate_isolated_dsn(base + suffix)
        for value in (base.replace("pgtest/", "pgtest:15432/"), base.replace("pgtest/", "pgtest:bad/"), base + "\n", None):
            with self.assertRaises(RecoveryError):
                validate_isolated_dsn(value)

    def test_object_keys_have_one_canonical_project_local_form(self):
        objects = IsolatedObjectDirectory(ROOT / ".runtime/recovery-tests")
        for key in ("", "a//b", "a/./b", "a/../b", "/absolute", "a"+chr(92)+"b", "a"+chr(0)+"b", None):
            with self.subTest(key=key), self.assertRaises(RecoveryError):
                objects.key_path(key)


class RecoveryCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_waits_for_backend_exit_without_force_or_role_elevation(self):
        admin = type("Connection", (), {"fetchval": AsyncMock(return_value=True),
            "execute": AsyncMock(side_effect=[None, asyncpg.ObjectInUseError("closing backend"), None])})()
        name = "recovery_" + "a"*32 + "_source_test"
        with patch("tests.test_recovery.asyncio.sleep", new=AsyncMock()):
            await test_recovery.drop_owned_test_database(admin, name)
        statements = [call.args[0] for call in admin.execute.await_args_list]
        self.assertIn("ALLOW_CONNECTIONS false", statements[0])
        self.assertEqual(2, sum(query.startswith("DROP DATABASE") for query in statements))
        self.assertTrue(all("FORCE" not in query and "ROLE" not in query and "terminate" not in query for query in statements))

    async def test_cleanup_rejects_foreign_or_unrecognized_database_before_ddl(self):
        admin = type("Connection", (), {"fetchval": AsyncMock(return_value=False), "execute": AsyncMock()})()
        for name in ("production", "recovery_"+"a"*32+"_source_test"):
            with self.assertRaises(RuntimeError):
                await test_recovery.drop_owned_test_database(admin, name)
        admin.execute.assert_not_awaited()


class RecoveryOperationsTests(unittest.TestCase):
    def valid_cleanup(self):
        now = datetime(2026, 10, 27, 17, tzinfo=timezone.utc)
        scope = {"kind": "media_objects", "dataset": "legacy-media", "entire_dataset": False,
                 "maximum_items": 100, "candidate_manifest_sha256": "a" * 64}
        return now, {"backup_reference": "backup/evidence-1", "backup_sha256": "b" * 64,
            "restore_evidence": "restore/isolated-pass", "reference_scan_evidence": "scan/evidence-1",
            "retention_review_evidence": "review/evidence-1", "scope_approval_reference": "approval/scope-1",
            "recovery_comparison_evidence": "compare/evidence-1", "scope": scope, "approved_scope": dict(scope),
            "business_references": 0, "restore_passed": True, "financial_and_media_equal": True,
            "reference_scan_at": now.isoformat(), "approval_expires_at": (now + timedelta(minutes=30)).isoformat()}

    def test_cleanup_requires_all_evidence_and_never_executes_deletion(self):
        now, good = self.valid_cleanup()
        report = cleanup_gate(good, now=now)
        self.assertTrue(report["eligible_for_separate_review"])
        self.assertFalse(report["execution_authorized"])
        self.assertFalse(report["deletion_performed"])
        for key in good:
            bad = deepcopy(good)
            del bad[key]
            with self.subTest(missing=key):
                self.assertFalse(cleanup_gate(bad, now=now)["eligible_for_separate_review"])

    def test_cleanup_rejects_whole_dataset_stale_scan_references_and_scope_changes(self):
        now, good = self.valid_cleanup()
        for key, value in (("business_references", 1), ("business_references", False),
                           ("restore_passed", False), ("financial_and_media_equal", False),
                           ("approval_expires_at", now.isoformat()),
                           ("reference_scan_at", (now - timedelta(hours=2)).isoformat())):
            bad = deepcopy(good)
            bad[key] = value
            self.assertFalse(cleanup_gate(bad, now=now)["eligible_for_separate_review"])
        for update in ({"entire_dataset": True}, {"maximum_items": 1001}, {"candidate_manifest_sha256": "missing"}):
            bad = deepcopy(good)
            bad["scope"].update(update)
            bad["approved_scope"] = dict(bad["scope"])
            self.assertFalse(cleanup_gate(bad, now=now)["eligible_for_separate_review"])
        changed = deepcopy(good)
        changed["scope"]["maximum_items"] = 50
        self.assertIn("cleanup_scope_not_explicitly_approved", cleanup_gate(changed, now=now)["reasons"])

    def test_recovery_plan_preserves_other_projects_and_orders_dependencies(self):
        document = {"project": "ai-football-v2", "backup_reference": "backup/verified",
            "restore_evidence": "restore/verified", "target_business_data": "empty_isolated_target",
            "live_orders_disabled": True, "required_ports": [3003], "services": [
                {"name": "platform-web", "compose_project": "ai-football-v2", "published_ports": [{"port": 3003}]},
                {"name": "another-web", "compose_project": "unrelated", "published_ports": [{"port": 8080}]}]}
        report = recovery_plan(document)
        self.assertEqual(["another-web"], report["preserve_services"])
        self.assertEqual([], report["blockers"])
        self.assertFalse(report["execution_performed"])
        self.assertFalse(report["production_restore_verified"])
        self.assertEqual([[], [1], [2], [3], [4], [5], [6], [7], [8]], [stage["requires"] for stage in report["stages"]])
        document["required_ports"] = [8080]
        self.assertIn("port_conflicts_require_resolution", recovery_plan(document)["blockers"])
        document["target_business_data"] = "existing_production"
        document["live_orders_disabled"] = False
        self.assertIn("existing_data_must_not_be_overwritten", recovery_plan(document)["blockers"])
        document["project"] = "ai_football"
        legacy = recovery_plan(document)
        self.assertIn("without_starting_old_models", legacy["stages"][5]["action"])
        self.assertIn("legacy_collector", legacy["stages"][6]["action"])
        with self.assertRaises(RecoveryError):
            recovery_plan({"project": "unrelated"})
