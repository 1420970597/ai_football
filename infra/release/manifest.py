"""Create an honest development release manifest from verified build outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from infra.android.package_policy import verify_network_permissions


DEVELOPMENT_VERSION = "2.0.0-dev"
ANDROID_APPLICATION_ID = "com.aifootball.platform"
ANDROID_VERSION_CODE = "20000"
COMPONENT_SERVICES = {
    "api": ("api", "migrate", "review-worker"), "collector": ("collector",),
    "executor": ("executor",), "mcp": ("hermes-mcp",), "web": ("web",),
    "storage-tunnel": ("storage-tunnel",),
}


def compose_override(manifest: dict) -> dict:
    """Pin runtime services to the exact candidate assets without carrying secrets."""
    images = manifest.get("images", {})
    if set(images) != set(COMPONENT_SERVICES):
        raise ValueError("complete candidate images are required for Compose")
    if manifest.get("version") != DEVELOPMENT_VERSION or manifest.get("channel") != "development" or manifest.get("production_ready") is not False:
        raise ValueError("only an explicitly labelled development candidate is supported")
    commit = manifest.get("source_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("candidate source commit is missing")
    services = {}
    for component, names in COMPONENT_SERVICES.items():
        reference = images[component]
        if not isinstance(reference, str) or not re.fullmatch(r"ghcr\.io/[a-z0-9_./-]+@sha256:[0-9a-f]{64}", reference):
            raise ValueError("Compose candidate requires immutable image digests")
        for name in names:
            services[name] = {"image": reference}
    hermes = manifest.get("hermes", "")
    if not isinstance(hermes, str) or not re.fullmatch(r"nousresearch/hermes-agent@sha256:[0-9a-f]{64}", hermes):
        raise ValueError("Hermes image digest is missing")
    services["hermes"] = {"image": hermes}
    return {"x-release-source": commit, "x-development-only": True, "services": services}


def make_manifest(root: Path, commit: str, images: dict, artifacts: Path) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("source commit must be a full SHA-1")
    required = json.loads((root / "infra/release/images.json").read_text())
    if set(images) != set(required):
        raise ValueError("the complete project image set is required")
    for name, reference in images.items():
        if not re.fullmatch(r"ghcr\.io/[a-z0-9_./-]+@sha256:[0-9a-f]{64}", reference):
            raise ValueError(f"immutable registry digest missing: {name}")
    assets = {}
    for kind in ("debug", "unsigned"):
        file = artifacts / f"ballnews-{DEVELOPMENT_VERSION}-{kind}.apk"
        if not file.is_file() or file.is_symlink():
            raise ValueError("both clearly named development APKs are required")
        assets[file.name] = {"sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                             "bytes": file.stat().st_size, "signing": "debug" if kind == "debug" else "unsigned",
                             "package_name": ANDROID_APPLICATION_ID + (".dev" if kind == "debug" else ""),
                             "version_code": int(ANDROID_VERSION_CODE),
                             "version_name": DEVELOPMENT_VERSION + ("+debug" if kind == "debug" else "")}
    parent = (artifacts / "source-parent-commit.txt").read_text().strip()
    dirty = (artifacts / "source-worktree-status.txt").read_text().strip()
    if parent != commit or dirty:
        raise ValueError("APKs must be built from the exact clean release source commit")
    package = (artifacts / 'package-verification.txt').read_text()
    _validate_android_package(package)
    config_paths = ['integrations/hermes/config.yaml', 'integrations/hermes/AGENTS.md',
                    'integrations/hermes/runner.py', 'integrations/hermes/skills/expert-review/SKILL.md']
    configuration = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in config_paths}
    return {"version": DEVELOPMENT_VERSION, "source_commit": commit, "channel": "development",
            "compose_image_tag": f"{DEVELOPMENT_VERSION}-{commit}",
            "production_ready": False, "images": images, "assets": assets,
            "hermes_configuration_sha256": configuration,
            "hermes": "nousresearch/hermes-agent@sha256:9c5561579f3ffdf333401797094ac3a743738d6c25d40f4331c7bf77b694238d",
            "limits": ["Real providers and production deployment remain unverified.",
                       "Debug and unsigned APKs are not production signing identities."]}


def _validate_android_package(output: str) -> None:
    """Validate aapt metadata without trusting a filename or Gradle source alone."""
    expected = (f"name='{ANDROID_APPLICATION_ID}'", f"versionCode='{ANDROID_VERSION_CODE}'",
                f"versionName='{DEVELOPMENT_VERSION}'")
    package_line = next((line for line in output.splitlines()
                         if line.startswith("package: ") and all(item in line for item in expected)), "")
    if not package_line or any(item not in package_line for item in expected):
        raise ValueError("Android package id, version code or version name is invalid")
    verify_network_permissions(output)
    if "CN=Android Debug" not in output or "Verified using v2 scheme (APK Signature Scheme v2): true" not in output:
        raise ValueError("development package signing verification is missing")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compose-output", type=Path)
    args = parser.parse_args()
    result = make_manifest(Path.cwd(), args.commit, json.loads(args.images.read_text()), args.artifacts)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if args.compose_output:
        args.compose_output.write_text(json.dumps(compose_override(result), ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
