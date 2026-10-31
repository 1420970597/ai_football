"""Bind a device probe to an explicit locally verified, signed APK."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re

from infra.android.package_policy import verify_network_permissions
from infra.android.release_policy import validate_identity, verify_package


def local_file(root: Path, path: Path) -> Path:
    root = root.resolve()
    original = path if path.is_absolute() else root / path
    resolved = original.resolve()
    if root not in resolved.parents or not resolved.is_file():
        raise ValueError('device artifacts must be project-local regular files')
    for item in [original, *original.parents]:
        if item == root:
            break
        if item.is_symlink():
            raise ValueError('device artifacts cannot use symbolic links')
    return resolved


@dataclass(frozen=True)
class DeviceArtifact:
    package_name: str
    version_name: str
    version_code: int
    source_commit: str
    apk_sha256: str
    certificate_sha256: str
    manifest_sha256: str
    channel: str
    apk_path: Path

    def evidence(self):
        return {key: value for key, value in asdict(self).items() if key != 'apk_path'}


def verify_device_artifact(root: Path, manifest: Path, apk: Path, package_output: Path) -> DeviceArtifact:
    manifest = local_file(root, manifest)
    apk = local_file(root, apk)
    package_output = local_file(root, package_output)
    record = json.loads(manifest.read_text())
    if not isinstance(record, dict):
        raise ValueError('device manifest must be an object')
    output = package_output.read_text()
    digest = hashlib.sha256(apk.read_bytes()).hexdigest()
    commit = record.get('source_commit', '')
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('device manifest requires a full source commit')
    channel = record.get('channel')
    if channel in {'signed-candidate', 'stable-eligible'}:
        version = record['version']
        code = record['version_code']
        validate_identity(version, code, commit)
        package = 'com.aifootball.platform'
        if record.get('package_name') != package or record.get('apk_sha256') != digest:
            raise ValueError('device APK does not match the signed manifest')
        signer = verify_package(output, version, code, record['certificate_sha256'])
    elif channel == 'development':
        asset = record.get('assets', {}).get(apk.name, {})
        package = 'com.aifootball.platform.dev'
        version = '2.0.0-dev+debug'
        code = 20000
        expected = {'sha256': digest, 'signing': 'debug', 'package_name': package,
                    'version_code': code, 'version_name': version}
        if record.get('version') != '2.0.0-dev' or any(asset.get(k) != v for k, v in expected.items()):
            raise ValueError('device probe requires the exact debug asset from the development manifest')
        verify_network_permissions(output)
        lines = [line for line in output.splitlines() if line.startswith('package: ')]
        expected_tokens = [f"name='{package}'", f"versionCode='{code}'", f"versionName='{version}'"]
        if sum(all(token in line for token in expected_tokens) for line in lines) != 1:
            raise ValueError('development APK package metadata mismatch')
        signers = re.findall(r'^Signer #[0-9]+ certificate SHA-256 digest: ([0-9a-fA-F]{64})$', output, re.M)
        if len(signers) != 1 or 'CN=Android Debug' not in output or 'Verified using v2 scheme (APK Signature Scheme v2): true' not in output:
            raise ValueError('development APK debug signing verification missing')
        signer = signers[0].lower()
    else:
        raise ValueError('device probe requires a verified signed or debug manifest')
    return DeviceArtifact(package, version, code, commit, digest, signer,
                          hashlib.sha256(manifest.read_bytes()).hexdigest(), channel, apk)


def installed_version(output: str) -> dict:
    codes = set(re.findall(r'\bversionCode=(\d+)\b', output))
    names = set(re.findall(r'^\s*versionName=([^\r\n]+)', output, re.M))
    if len(codes) != 1 or len(names) != 1:
        raise ValueError('selected package installed version is missing or ambiguous')
    return {'version_code': int(codes.pop()), 'version_name': names.pop().strip()}


def validate_upgrade(previous: DeviceArtifact, current: DeviceArtifact) -> None:
    if previous.package_name != current.package_name or previous.certificate_sha256 != current.certificate_sha256:
        raise ValueError('upgrade requires the same verified package and signing identity')
    if previous.version_code >= current.version_code:
        raise ValueError('upgrade version code must increase')
