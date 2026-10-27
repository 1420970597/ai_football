"""只读审计项目配置与旧归档，输出仅包含键名、结构、权限和状态。

This command intentionally never extracts an archive member to the host and never
opens a network connection.  It accepts an archive outside the repository as a
read-only input and writes only the redacted JSON report inside the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile
from typing import Any
from urllib.parse import urlparse

try:
    from .reuse_inventory import parse_env
except ImportError:  # direct `python infra/config/read_only_audit.py`
    from reuse_inventory import parse_env


SAMPLE_PATHS = (
    "ai_football/.env",
    "ai_football/.env.inference",
    "ai_football/.env.before-app-signing",
    "ai_football/.gitignore",
    "ai_football/client/credentials.json",
    "ai_football/client/s3-ca.crt",
    "ai_football/collector/leyu_client.py",
    "ai_football/store/runtime_settings_db.py",
    "ai_football/service/live_expert.py",
    "ai_football/docs/architecture/leyu-api-protocol.md",
    "ai_football/docker/docker-compose.yml",
    "ai_football/docker/docker-compose.storage.yml",
)

SENSITIVE_BASENAMES = {
    ".env",
    ".env.inference",
    ".env.before-app-signing",
    "credentials.json",
    "runtime.env",
}

REQUIRED_KEYS = {
    "core": ("DATABASE_URL", "DATABASE_TRANSPORT", "APP_SESSION_SECRET", "DATA_ENCRYPTION_KEY"),
    "storage": ("S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET", "S3_REGION", "S3_CA_FILE"),
    "hermes": ("HERMES_MODEL", "HERMES_PROVIDER_BASE_URL", "HERMES_PROVIDER_API_KEY"),
    "service": ("EXPERT_MCP_TOKEN", "COLLECTOR_INTERNAL_TOKEN", "EXECUTOR_INTERNAL_TOKEN"),
}

URL_KEYS = {"DATABASE_URL", "S3_ENDPOINT_URL", "HERMES_PROVIDER_BASE_URL"}
BOOL_KEYS = {"COOKIE_SECURE", "LIVE_ORDERS_ENABLED", "EXECUTOR_LIVE_ORDERS_ENABLED"}
INT_KEYS = {"QUOTE_MAX_AGE_SECONDS", "REVIEW_WORKER_CONCURRENCY"}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archive_path_kind(name: str) -> str:
    lowered = name.casefold()
    if ".env" in lowered or "credential" in lowered or "secret" in lowered:
        return "configuration"
    if "leyu" in lowered or "telegram" in lowered or "protocol" in lowered:
        return "protocol"
    if "compose" in lowered or "dockerfile" in lowered or "deploy" in lowered:
        return "deployment"
    if "report" in lowered or "research" in lowered:
        return "research"
    if name.startswith("ai_football/.git/"):
        return "git_metadata"
    return "other"


def _summarize_env(content: bytes) -> dict[str, Any]:
    values = parse_env(content.decode("utf-8"))
    return {
        "key_count": len(values),
        "keys": sorted(values),
        "nonempty_keys": sorted(key for key, value in values.items() if value),
        "empty_keys": sorted(key for key, value in values.items() if not value),
    }


def _summarize_json(content: bytes) -> dict[str, Any]:
    document = json.loads(content.decode("utf-8"))

    def shape(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): shape(item) for key, item in value.items()}
        if isinstance(value, list):
            return {"type": "list", "count": len(value)}
        return {"type": type(value).__name__, "present": bool(value)}

    return {"root_type": type(document).__name__, "shape": shape(document)}


def archive_report(archive: Path) -> dict[str, Any]:
    stat = archive.stat()
    result: dict[str, Any] = {
        "source": {"path": str(archive), "mode": oct(stat.st_mode & 0o777), "bytes": stat.st_size},
        "archive_sha256": None,
        "member_count": 0,
        "top_level": {},
        "categories": {},
        "samples": [],
        "security_findings": [],
    }
    # Hashing is deliberately explicit evidence; it does not copy or mutate the archive.
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    result["archive_sha256"] = digest.hexdigest()

    with tarfile.open(archive, mode="r:*") as source:
        members = source.getmembers()
        result["member_count"] = len(members)
        category_counts: dict[str, int] = {}
        top_level: dict[str, int] = {}
        by_name = {member.name: member for member in members}
        for member in members:
            category = _archive_path_kind(member.name)
            category_counts[category] = category_counts.get(category, 0) + 1
            top = member.name.split("/", 2)[1] if "/" in member.name else member.name
            top_level[top] = top_level.get(top, 0) + 1
        result["categories"] = dict(sorted(category_counts.items()))
        result["top_level"] = dict(sorted(top_level.items()))
        for name in SAMPLE_PATHS:
            member = by_name.get(name)
            if member is None:
                result["samples"].append({"path": name, "found": False})
                continue
            item: dict[str, Any] = {
                "path": name,
                "found": True,
                "type": "file" if member.isfile() else "other",
                "mode": oct(member.mode & 0o777),
                "bytes": member.size,
                "mtime": member.mtime,
            }
            if member.isfile() and member.size <= 5_000_000:
                content = source.extractfile(member).read()
                item["sha256"] = _sha256_bytes(content)
                if name.endswith(("/.env", ".env.inference", ".env.before-app-signing")):
                    item["structure"] = _summarize_env(content)
                elif name.endswith("credentials.json"):
                    item["structure"] = _summarize_json(content)
                if Path(name).name in SENSITIVE_BASENAMES and member.mode & 0o077:
                    result["security_findings"].append(
                        {"path": name, "kind": "sensitive_archive_member_not_private", "mode": oct(member.mode & 0o777)}
                    )
            result["samples"].append(item)
    return result


def _check_runtime_env(path: Path, project_root: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "present": False, "missing": sorted({key for group in REQUIRED_KEYS.values() for key in group})}
    mode = path.stat().st_mode & 0o777
    values = parse_env(path.read_text(encoding="utf-8"))
    statuses: dict[str, dict[str, Any]] = {}
    invalid: list[dict[str, str]] = []
    external_unverified: list[str] = []
    authorization_unverified: list[str] = []
    for category, keys in REQUIRED_KEYS.items():
        for key in keys:
            present = key in values and bool(values[key].strip())
            statuses[key] = {"category": category, "present": present}
            if not present:
                continue
            if key in URL_KEYS:
                parsed = urlparse(values[key])
                if parsed.scheme not in {"http", "https", "postgres", "postgresql"} or not parsed.netloc:
                    invalid.append({"key": key, "reason": "malformed_url"})
                else:
                    external_unverified.append(key)
            if key in BOOL_KEYS and values[key].casefold() not in {"true", "false"}:
                invalid.append({"key": key, "reason": "boolean_expected"})
            if key in INT_KEYS:
                try:
                    if int(values[key]) <= 0:
                        raise ValueError
                except ValueError:
                    invalid.append({"key": key, "reason": "positive_integer_expected"})
            if "TOKEN" in key or "SECRET" in key or "PASSWORD" in key or key.endswith("_KEY"):
                authorization_unverified.append(key)
    for key in sorted((BOOL_KEYS - set(statuses)) & values.keys()):
        if values[key].casefold() not in {"true", "false"}:
            invalid.append({"key": key, "reason": "boolean_expected"})
    for key in sorted((INT_KEYS - set(statuses)) & values.keys()):
        try:
            if int(values[key]) <= 0:
                raise ValueError
        except ValueError:
            invalid.append({"key": key, "reason": "positive_integer_expected"})
    path_keys = {"S3_CA_FILE", "LEYU_SIGNING_FILE", "LEYU_DISCOVERY_FILE"}
    referenced_files: list[dict[str, Any]] = []
    for key in sorted(path_keys & values.keys()):
        candidate = Path(values[key])
        if not candidate.is_absolute():
            candidate = project_root / candidate
        if not candidate.exists():
            invalid.append({"key": key, "reason": "referenced_file_missing"})
            referenced_files.append({"key": key, "present": False, "kind": "file"})
        elif not candidate.is_file():
            invalid.append({"key": key, "reason": "referenced_path_not_file"})
            referenced_files.append({"key": key, "present": True, "kind": "non_file"})
        elif key != "S3_CA_FILE" and candidate.stat().st_mode & 0o077:
            invalid.append({"key": key, "reason": "referenced_secret_file_not_private"})
            referenced_files.append({
                "key": key,
                "present": True,
                "kind": "secret_file",
                "private_mode": False,
            })
        else:
            # A CA certificate contains public trust material and must be
            # readable by the non-root API user; 0600 is not required.
            referenced_files.append({
                "key": key,
                "present": True,
                "kind": "ca_certificate" if key == "S3_CA_FILE" else "secret_file",
                "private_mode": not bool(candidate.stat().st_mode & 0o077),
            })
    return {
        "path": str(path),
        "present": True,
        "mode": oct(mode),
        "private_mode": mode == 0o600,
        "key_count": len(values),
        "statuses": statuses,
        "missing": sorted(key for key, status in statuses.items() if not status["present"]),
        "invalid": invalid,
        "external_unverified": sorted(set(external_unverified)),
        "authorization_unverified": sorted(set(authorization_unverified)),
        "referenced_files": referenced_files,
        "network_checked": False,
    }


def _source_policy(project_root: Path) -> dict[str, Any]:
    secret_paths: list[str] = []
    for candidate in project_root.rglob("*"):
        if not candidate.is_file() or ".git" in candidate.parts or ".runtime" in candidate.parts:
            continue
        relative = str(candidate.relative_to(project_root))
        if candidate.name == ".env.example":
            # The checked-in example is a redacted fixture, not runtime
            # credentials.  It remains subject to the repository secret scan.
            continue
        if candidate.is_symlink():
            try:
                resolved = candidate.resolve(strict=False)
                if project_root / ".runtime" in resolved.parents:
                    # A compatibility .env link may point at the protected
                    # runtime file; report it separately instead of treating
                    # the link itself as a copied secret.
                    continue
            except OSError:
                pass
        if re.search(r"(^|/)(\.env($|\.)|.*\.(pem|key|p12|jks|keystore)$)", relative, re.I):
            secret_paths.append(relative)
    docker_context_findings: list[dict[str, str]] = []
    for path in project_root.rglob("Dockerfile*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"COPY\s+.*(\.runtime|\.env|credentials|secret|\.pem|\.key)", text, re.I):
            docker_context_findings.append({"path": str(path.relative_to(project_root)), "kind": "secret_copy_instruction"})
    ignored = (project_root / ".gitignore").read_text(encoding="utf-8") if (project_root / ".gitignore").exists() else ""
    return {
        "repository_secret_paths_outside_runtime": sorted(secret_paths),
        "docker_context_findings": docker_context_findings,
        "runtime_ignored": ".runtime/" in ignored,
        "env_ignored": ".env" in ignored,
        "status": "review_required" if secret_paths or docker_context_findings else "static_checks_passed",
    }


def project_report(project_root: Path) -> dict[str, Any]:
    runtime = _check_runtime_env(project_root / ".runtime" / "runtime.env", project_root)
    policy = _source_policy(project_root)
    return {
        "project_root": str(project_root),
        "runtime": runtime,
        "source_policy": policy,
        "production_ready": False,
        "not_verified": ["external_reachability", "TLS_and_credentials", "authorization_expiry", "database_privileges", "remote_restore"],
    }


def write_report(output: Path, report: dict[str, Any]) -> None:
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.archive.is_file():
        raise SystemExit("archive input is not a regular file")
    root = args.project_root.resolve()
    output = args.output.resolve()
    if root not in output.parents:
        raise SystemExit("output must be inside project root")
    report = {"schema_version": 1, "archive": archive_report(args.archive), "project": project_report(root)}
    write_report(output, report)
    print(json.dumps({"status": "ok", "output": str(output), "member_count": report["archive"]["member_count"], "archive_findings": len(report["archive"]["security_findings"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
