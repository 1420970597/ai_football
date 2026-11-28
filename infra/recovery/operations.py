"""Read-only recovery sequencing and explicit cleanup evidence gates.

These functions never stop containers, write databases or delete objects.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

from .bundle import RecoveryError


def _evidence(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:/@-]{2,180}", value) is not None


def recovery_plan(document: dict) -> dict:
    """Render a reviewable ordered plan; unknown owners cannot be stopped."""
    project = document.get("project")
    if project not in {"ai-football", "ai-football-v1", "ai-football-v2", "ai_football"}:
        raise RecoveryError("explicit_project_ownership_required")
    services = document.get("services", [])
    if not isinstance(services, list) or len(services) > 100:
        raise RecoveryError("invalid_recovery_service_inventory")
    ports, conflicts, owned, unknown = {}, [], [], []
    for service in services:
        if not isinstance(service, dict) or not _evidence(service.get("name")):
            raise RecoveryError("invalid_recovery_service_inventory")
        name = service["name"]
        current_owner = service.get("compose_project")
        if current_owner == project:
            owned.append(name)
        else:
            unknown.append(name)
        for port in service.get("published_ports", []):
            if (not isinstance(port, dict) or port.get("protocol", "tcp") not in {"tcp", "udp"}
                    or type(port.get("port")) is not int or not 1 <= port["port"] <= 65535):
                raise RecoveryError("invalid_recovery_port_inventory")
            key = (port.get("protocol", "tcp"), port["port"])
            # Conservatively require manual resolution even when bind addresses
            # differ; wildcard/IPv6 host binding behavior must not be guessed.
            if key in ports and ports[key] != name:
                conflicts.append({"protocol": key[0], "port": key[1], "owners": sorted({ports[key], name})})
            ports[key] = name
    requested = document.get("required_ports", [])
    for port in requested:
        if type(port) is not int or not 1 <= port <= 65535:
            raise RecoveryError("invalid_recovery_port_inventory")
        holder = ports.get(("tcp", port))
        if holder and holder not in owned:
            conflicts.append({"protocol": "tcp", "port": port, "owners": [holder], "reason": "foreign_or_unknown_owner"})
    blockers = []
    if not _evidence(document.get("backup_reference")):
        blockers.append("verified_backup_reference_missing")
    if not _evidence(document.get("restore_evidence")):
        blockers.append("isolated_restore_evidence_missing")
    if document.get("target_business_data") != "empty_isolated_target":
        blockers.append("existing_data_must_not_be_overwritten")
    if document.get("live_orders_disabled") is not True:
        blockers.append("live_orders_must_remain_disabled")
    if conflicts:
        blockers.append("port_conflicts_require_resolution")
    stages = [
        {"order": 1, "action": "verify_backup_media_and_independent_key_references", "requires": []},
        {"order": 2, "action": "keep_public_service_read_only_and_drain_writers", "requires": [1]},
        {"order": 3, "action": "verify_owned_ports_and_storage_tunnel_identity", "requires": [2]},
        {"order": 4, "action": "restore_into_empty_isolated_database_and_media_prefix", "requires": [3]},
        {"order": 5, "action": "compare_financial_authorization_and_media_evidence", "requires": [4]},
        {"order": 6, "action": "start_api_then_mcp_then_hermes_then_review_worker", "requires": [5]},
        {"order": 7, "action": "start_single_collector_from_verified_checkpoint", "requires": [6]},
        {"order": 8, "action": "verify_read_only_pages_before_ingress_switch", "requires": [7]},
        {"order": 9, "action": "reconcile_unknown_orders_before_any_executor_enable", "requires": [8]},
    ]
    if project in {"ai-football-v1", "ai_football"}:
        stages[5]["action"] = "verify_legacy_storage_api_and_reused_auth_without_starting_old_models"
        stages[6]["action"] = "start_one_legacy_collector_after_checkpoint_and_session_ownership_review"
        stages[7]["action"] = "verify_legacy_read_only_web_on_nonconflicting_port_before_ingress_switch"
    return {"project": project, "owned_services": sorted(owned), "preserve_services": sorted(unknown),
            "port_conflicts": conflicts, "blockers": blockers, "stages": stages,
            "plan_reviewable": not blockers, "execution_performed": False, "production_restore_verified": False}


def cleanup_gate(document: dict, *, now: datetime | None = None) -> dict:
    """All cleanup prerequisites are required; this gate has no delete path."""
    reasons = []
    checked = now or datetime.now(timezone.utc)
    for name in ("backup_reference", "backup_sha256", "restore_evidence", "reference_scan_evidence",
                 "retention_review_evidence", "scope_approval_reference", "recovery_comparison_evidence"):
        if not _evidence(document.get(name)):
            reasons.append(name + "_missing")
    if not re.fullmatch(r"[a-f0-9]{64}", str(document.get("backup_sha256") or "")):
        reasons.append("backup_checksum_invalid")
    scope = document.get("scope")
    approved = document.get("approved_scope")
    if not isinstance(scope, dict) or scope != approved:
        reasons.append("cleanup_scope_not_explicitly_approved")
    elif (scope.get("kind") not in {"database_rows", "media_objects"}
          or not _evidence(scope.get("dataset")) or scope.get("entire_dataset") is not False
          or type(scope.get("maximum_items")) is not int or not 1 <= scope["maximum_items"] <= 1000
          or not re.fullmatch(r"[a-f0-9]{64}", str(scope.get("candidate_manifest_sha256") or ""))):
        reasons.append("cleanup_scope_must_be_bounded_manifest")
    if type(document.get("business_references")) is not int or document.get("business_references") != 0:
        reasons.append("business_references_not_proven_zero")
    if document.get("restore_passed") is not True or document.get("financial_and_media_equal") is not True:
        reasons.append("restore_or_business_comparison_not_verified")
    for name in ("reference_scan_at", "approval_expires_at"):
        try:
            stamp = datetime.fromisoformat(str(document.get(name)).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError()
            age = (checked - stamp).total_seconds()
            if (name == "reference_scan_at" and not 0 <= age <= 3600) or (name == "approval_expires_at" and age >= 0):
                reasons.append(name + "_stale")
        except (TypeError, ValueError):
            reasons.append(name + "_invalid")
    fingerprint = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"eligible_for_separate_review": not reasons, "reasons": sorted(set(reasons)),
            "evidence_fingerprint": fingerprint, "checked_at": checked.isoformat(),
            "deletion_performed": False, "execution_authorized": False}
