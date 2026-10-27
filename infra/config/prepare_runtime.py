"""Project-only, idempotent runtime directory preparation and permission audit.

No credentials are generated, copied, replaced or printed. The explicit signing
group operation changes only project-owned signing/discovery inode permissions.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat


PRIVATE_DIRECTORIES = (".runtime", ".runtime/tunnel", ".runtime/recovery", ".runtime/config-audit")
REFERENCES = {
    "runtime_environment": (".runtime/runtime.env", None),
    "s3_ca": (".runtime/s3-ca.crt", 10001),
    "collector_signing": (".runtime/leyu-app-signing.json", 10003),
    "collector_discovery": (".runtime/leyu-app-discovery.json", 10003),
    "executor_signing": (".runtime/leyu-app-signing.json", 10004),
    "executor_discovery": (".runtime/leyu-app-discovery.json", 10004),
}
SIGNING_GROUP = 10005
SIGNING_REFERENCES = (".runtime/leyu-app-signing.json", ".runtime/leyu-app-discovery.json")


class RuntimePreparationError(ValueError):
    pass


def within_project(root: Path, relative: str) -> Path:
    root = root.resolve()
    candidate = root / relative
    if Path(relative).is_absolute() or candidate.is_symlink():
        raise RuntimePreparationError("runtime_path_must_be_project_owned")
    resolved = candidate.resolve()
    if root not in resolved.parents:
        raise RuntimePreparationError("runtime_path_must_be_project_owned")
    for parent in candidate.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise RuntimePreparationError("runtime_symlink_rejected")
    return candidate


def prepare_runtime(root: Path, *, apply: bool = False, signing_group: bool = False) -> dict:
    root = root.resolve()
    paths = {relative: within_project(root, relative) for relative in PRIVATE_DIRECTORIES}
    references = {name: (within_project(root, relative), uid) for name, (relative, uid) in REFERENCES.items()}
    # Validate all existing targets before changing even the first directory.
    for path in paths.values():
        if path.exists() and not path.is_dir():
            raise RuntimePreparationError("runtime_directory_conflicts_with_file")
    for path, _ in references.values():
        if path.exists() and not path.is_file():
            raise RuntimePreparationError("runtime_reference_not_regular_file")
    if signing_group:
        if not apply:
            raise RuntimePreparationError("signing_group_requires_explicit_apply")
        if os.geteuid()!=0:
            raise RuntimePreparationError("signing_group_requires_runtime_owner")
        targets=[within_project(root,relative) for relative in SIGNING_REFERENCES]
        if any(not path.is_file() or path.stat().st_nlink!=1 for path in targets):
            raise RuntimePreparationError("private_signing_files_required")
        # A single dedicated group is added only to collector and executor.
        # Existing Docker binds retain their inode; no secret content is copied.
        for path in targets:
            os.chown(path,0,SIGNING_GROUP)
            path.chmod(0o440)
    directories = []
    for relative, path in paths.items():
        if apply:
            path.mkdir(mode=0o700, exist_ok=True)
            path.chmod(0o700)
        directories.append({"path": relative, "present": path.is_dir(),
                            "private": path.is_dir() and stat.S_IMODE(path.stat().st_mode) == 0o700})
    files = {}
    for name, (path, uid) in references.items():
        if not path.exists():
            files[name] = {"present": False, "state": "unconfigured"}
            continue
        info = path.stat()
        mode = stat.S_IMODE(info.st_mode)
        # Docker file binds expose the leaf directly, so host parent traversal
        # is irrelevant. Service uid/gid must still read the mounted inode.
        readable = None if uid is None else bool(
            (info.st_uid == uid and mode & stat.S_IRUSR) or
            ((info.st_gid == uid or (name.endswith(("signing","discovery")) and info.st_gid==SIGNING_GROUP)) and mode & stat.S_IRGRP)
            or mode & stat.S_IROTH)
        private = not bool(mode & 0o077)
        group_private=(name.endswith(("signing","discovery")) and info.st_uid==0 and info.st_gid==SIGNING_GROUP and mode==0o440)
        acceptable = bool(mode & 0o444) and (name == "s3_ca" or private or group_private) and not bool(mode & 0o022)
        files[name] = {"present": True, "mode": oct(mode), "private": private,
                       "readable_by_service": readable, "read_only_mount_required": True,
                       "required_supplementary_group": SIGNING_GROUP if group_private else None,
                       "state": "ready" if acceptable and readable is not False else "permission_review_required"}
    return {"applied": apply, "directories": directories, "references": files,
            "secrets_created": False, "secrets_replaced": False, "signing_group_applied": signing_group,
            "production_ready": False, "network_checked": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--signing-group", action="store_true")
    args = parser.parse_args()
    try:
        result = prepare_runtime(args.root, apply=args.apply, signing_group=args.signing_group)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, RuntimePreparationError) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, RuntimePreparationError) else type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
