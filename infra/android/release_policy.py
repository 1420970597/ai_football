"""Validate signed Android candidates and fail closed on missing release evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from infra.android.package_policy import verify_network_permissions

REQUIRED_SCOPES = frozenset({
    "production_https", "remote_restore", "upstream_readonly", "expert_review",
    "statistics", "browser_desktop_mobile", "android_device", "android_upgrade",
})


def validate_identity(version: str, version_code: int, commit: str) -> None:
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-rc\.[1-9][0-9]*)?", version):
        raise ValueError("signed version must be a semantic version or numbered release candidate")
    if not 1 <= version_code <= 2100000000 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("positive Android version code and complete source commit are required")


def verify_package(output: str, version: str, version_code: int, certificate: str) -> str:
    expected = {"name": "com.aifootball.platform", "versionCode": str(version_code), "versionName": version}
    package_lines = [line for line in output.splitlines() if line.startswith("package: ")]
    if len(package_lines) != 1 or any(f"{key}='{value}'" not in package_lines[0] for key, value in expected.items()):
        raise ValueError("signed APK identity does not match the requested release")
    verify_network_permissions(output)
    digests = re.findall(r"^Signer #[0-9]+ certificate SHA-256 digest: ([0-9a-fA-F]{64})$", output, re.M)
    normalized = certificate.replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized) or len(digests) != 1 or digests[0].lower() != normalized:
        raise ValueError("APK signer does not match the externally recorded signing identity")
    if "CN=Android Debug" in output or "Verified using v2 scheme (APK Signature Scheme v2): true" not in output:
        raise ValueError("a verified non-debug v2 APK signature is required")
    if "application-debuggable" in output or "application-testOnly" in output:
        raise ValueError("debuggable and test-only APKs cannot enter the signing channel")
    return normalized


def release_evidence(root: Path, directory: Path, commit: str, version: str, apk_sha256: str | None = None) -> list[dict]:
    """Require current, source-bound, successful evidence with hashed local artifacts."""
    root = root.resolve()
    directory = directory.resolve()
    if root not in directory.parents or not directory.is_dir():
        raise ValueError("release evidence must be a project-local directory")
    accepted = {}
    for file in sorted(directory.glob("*.json")):
        if file.is_symlink():
            raise ValueError("release evidence records cannot be symbolic links")
        record = json.loads(file.read_text())
        if not isinstance(record, dict):
            raise ValueError("release evidence record must be an object")
        scope = record.get("scope")
        if scope not in REQUIRED_SCOPES:
            continue
        if scope in accepted or record.get("status") != "passed" or record.get("exit_code") != 0:
            raise ValueError("release evidence is duplicated or contains a failed check")
        if record.get("source_commit") != commit or record.get("version") != version or record.get("real_environment") is not True:
            raise ValueError("release evidence must cover this source/version in a real authorized environment")
        if scope in {"android_device", "android_upgrade"} and (not apk_sha256 or record.get("apk_sha256") != apk_sha256):
            raise ValueError("Android device and upgrade evidence must cover this exact signed APK")
        try:
            checked = datetime.fromisoformat(record["checked_at"].replace("Z", "+00:00"))
            if checked.utcoffset() is None:
                raise ValueError
            age = (datetime.now(timezone.utc) - checked).total_seconds()
            if not 0 <= age <= 7 * 86400:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("release evidence timestamp is invalid or older than seven days") from None
        if not isinstance(record.get("command"), str) or not record["command"].strip() or not record.get("artifacts"):
            raise ValueError("release evidence requires the actual command and artifact hashes")
        for item in record["artifacts"]:
            original_path = root / item["path"]
            path = original_path.resolve()
            if root not in path.parents or not path.is_file() or original_path.is_symlink():
                raise ValueError("release artifacts must be regular project-local files")
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("release evidence artifact hash mismatch")
        accepted[scope] = {"scope": scope, "record_sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    if set(accepted) != REQUIRED_SCOPES:
        raise ValueError("release gate missing: " + ", ".join(sorted(REQUIRED_SCOPES - set(accepted))))
    if "-" in version:
        raise ValueError("a prerelease version cannot pass the stable release gate")
    return list(accepted.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--version", required=True)
    parser.add_argument("--version-code", type=int, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--package-output", type=Path)
    parser.add_argument("--certificate-sha256")
    parser.add_argument("--apk", type=Path)
    parser.add_argument("--worktree-status", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    try:
        validate_identity(args.version, args.version_code, args.commit)
        if bool(args.package_output) != bool(args.certificate_sha256):
            raise ValueError("package verification output and expected certificate must be supplied together")
        if args.evidence and not args.package_output:
            raise ValueError("stable release gate also requires the verified signed package")
        if args.package_output and (not args.apk or not args.worktree_status):
            raise ValueError("signed candidate requires its APK and source worktree evidence")
        if args.apk and (not args.apk.is_file() or args.apk.is_symlink()):
            raise ValueError("signed APK must be a regular file")
        dirty = bool(args.worktree_status.read_text().strip()) if args.worktree_status else True
        if args.evidence and dirty:
            raise ValueError("stable release source must be clean")
        signer = verify_package(args.package_output.read_text(), args.version, args.version_code, args.certificate_sha256) if args.package_output else None
        apk_sha256 = hashlib.sha256(args.apk.read_bytes()).hexdigest() if args.apk else None
        evidence = release_evidence(args.root, args.evidence, args.commit, args.version, apk_sha256) if args.evidence else None
        print(json.dumps({"channel": "stable-eligible" if evidence else "signed-candidate" if signer else "identity-only",
                          "production_ready": bool(evidence and signer), "source_commit": args.commit,
                          "version": args.version, "version_code": args.version_code,
                          "package_name": "com.aifootball.platform",
                          "certificate_sha256": signer, "source_clean": not dirty,
                          "apk_sha256": apk_sha256,
                          "release_evidence": evidence}, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
