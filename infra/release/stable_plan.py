"""Read-only stable release planning; never builds, uploads, tags or publishes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from infra.android.device_artifact import local_file, verify_device_artifact
from infra.android.release_policy import release_evidence
from infra.release.manifest import COMPONENT_SERVICES

HERMES_IMAGE = 'nousresearch/hermes-agent@sha256:9c5561579f3ffdf333401797094ac3a743738d6c25d40f4331c7bf77b694238d'
CONFIGURATION_PATHS = ('docker-compose.yml', 'integrations/hermes/config.yaml',
                       'integrations/hermes/AGENTS.md', 'integrations/hermes/runner.py',
                       'integrations/hermes/skills/expert-review/SKILL.md')
IMAGE_PATTERN = re.compile(r'ghcr\.io/[a-z0-9_./-]+@sha256:[0-9a-f]{64}\Z')


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_plan(root: Path, *, manifest: Path, apk: Path, package_output: Path,
              worktree_status: Path, image_inspect: Path, evidence: Path, notes: Path) -> dict:
    root = root.resolve()
    selected = verify_device_artifact(root, manifest, apk, package_output)
    source_manifest = json.loads(local_file(root, manifest).read_text())
    if selected.channel not in {'signed-candidate', 'stable-eligible'} or '-' in selected.version_name:
        raise ValueError('stable plan requires a stable-version externally signed APK')
    status_file = local_file(root, worktree_status)
    if status_file.read_text().strip() or source_manifest.get('source_clean') is not True:
        raise ValueError('stable plan source must be clean and recorded in its signing manifest')
    notes_file = local_file(root, notes)
    if not notes_file.read_text().strip():
        raise ValueError('stable release notes must be nonempty')
    image_file = local_file(root, image_inspect)
    images = json.loads(image_file.read_text())
    if not isinstance(images, dict) or set(images) != set(COMPONENT_SERVICES):
        raise ValueError('stable plan requires all six locally pulled image inspection records')
    references, image_provenance = {}, {}
    for component, row in sorted(images.items()):
        if not isinstance(row, dict):
            raise ValueError('image inspection record must be an object')
        reference = row.get('reference')
        inspection = row.get('inspection')
        if not isinstance(reference, str) or not IMAGE_PATTERN.fullmatch(reference) or not isinstance(inspection, dict):
            raise ValueError('stable images require immutable registry digests and inspection metadata')
        labels = inspection.get('Config', {}).get('Labels') or {}
        if (labels.get('org.opencontainers.image.revision') != selected.source_commit
                or labels.get('org.opencontainers.image.version') != selected.version_name):
            raise ValueError('every stable image must match the exact signed APK source and version')
        if reference not in inspection.get('RepoDigests', []) or not re.fullmatch(r'sha256:[0-9a-f]{64}', inspection.get('Id', '')):
            raise ValueError('image inspection must contain its local content ID and requested registry digest')
        if not reference.split('@', 1)[0].endswith('/ai-football-' + component):
            raise ValueError('stable image component repository mismatch')
        references[component] = reference
        image_provenance[component] = {'local_image_id': inspection['Id'],
            'inspection_sha256': hashlib.sha256(json.dumps(inspection, sort_keys=True).encode()).hexdigest()}
    accepted = release_evidence(root, evidence, selected.source_commit, selected.version_name, selected.apk_sha256)
    configuration = {name: digest(local_file(root, Path(name))) for name in CONFIGURATION_PATHS}
    migrations = sorted((root / 'services/api/migrations').glob('*.sql'))
    if not migrations:
        raise ValueError('stable plan requires the current ordered migration contract')
    migration_hashes = {str(path.relative_to(root)): digest(local_file(root, path)) for path in migrations}
    services = {name: {'image': references[component]} for component, names in COMPONENT_SERVICES.items() for name in names}
    services['hermes'] = {'image': HERMES_IMAGE}
    return {'schema_version': 1, 'publication_state': 'validated_dry_run', 'published': False,
            'production_ready': False, 'version': selected.version_name, 'tag': 'v' + selected.version_name,
            'source_commit': selected.source_commit, 'apk': selected.evidence(),
            'images': references, 'image_provenance': image_provenance,
            'hermes_image': HERMES_IMAGE, 'configuration_sha256': configuration,
            'migration_sha256': migration_hashes, 'release_evidence': accepted,
            'notes_sha256': digest(notes_file), 'worktree_status_sha256': digest(status_file),
            'compose_override': {'x-release-source': selected.source_commit, 'services': services},
            'remaining_external_actions': ['revalidate_current_remote_tag_absence',
                'pull_digests_in_clean_deployment_and_validate_readonly_business',
                'create_immutable_tag_and_release_with_reviewed_assets',
                'read_back_remote_commit_and_asset_hashes'],
            'limitations': ['Local inspection and evidence are validated; no remote publication or installation is performed.']}


def validate_plan(plan: dict, **inputs) -> dict:
    if plan != make_plan(**inputs):
        raise ValueError('release plan differs from current validated local inputs')
    return {'publication_state': 'validated_dry_run', 'published': False, 'source_commit': plan['source_commit'],
            'version': plan['version'], 'apk_sha256': plan['apk']['apk_sha256']}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'validate'])
    parser.add_argument('--root', type=Path, default=Path.cwd())
    for name in ('manifest', 'apk', 'package-output', 'worktree-status', 'image-inspect', 'evidence', 'notes'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--plan', type=Path, help='Existing project-local plan, required only for validate')
    args = parser.parse_args()
    inputs = {key: getattr(args, key) for key in ('root','manifest','apk','package_output','worktree_status','image_inspect','evidence','notes')}
    try:
        if args.action == 'validate':
            if not args.plan:
                raise ValueError('validate requires an existing local plan')
            result = validate_plan(json.loads(local_file(args.root, args.plan).read_text()), **inputs)
        else:
            if args.plan:
                raise ValueError('plan writes JSON to stdout; do not supply an input plan')
            result = make_plan(**inputs)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'publication_state': 'rejected', 'published': False, 'reason': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
