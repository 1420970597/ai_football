"""Explicit project-only preparation and evidence-gated switch plan; no remote commands."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat

ROOT = '/root/ai_football'
ROLES = ('collector', 'executor', 'tunnel')


def project_path(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = root / relative
    if Path(relative).is_absolute() or root not in path.resolve().parents:
        raise ValueError('remote_project_path_required')
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink():
            raise ValueError('remote_project_symlink_rejected')
    return path


def prepare(root: Path, *, apply=False) -> dict:
    path = project_path(root, '.runtime/remote')
    if path.exists() and not path.is_dir():
        raise ValueError('remote_directory_conflict')
    if apply:
        if os.geteuid() != 0:
            raise ValueError('remote_prepare_owner_required')
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(path, 0, 10005)
        path.chmod(0o2750)
    info = path.stat() if path.exists() else None
    return {'applied': apply, 'directory_ready': bool(info and info.st_uid == 0 and info.st_gid == 10005
                                                    and stat.S_IMODE(info.st_mode) == 0o2750),
            'secrets_generated': False, 'remote_writes': False}


def validate_manifest(value: dict) -> list[str]:
    errors = []
    commit = value.get('source_commit', '')
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        errors.append('source_commit_invalid')
    if value.get('remote_root') != ROOT:
        errors.append('remote_project_root_unverified')
    version = value.get('configuration_version')
    if type(version) is not int or version < 1:
        errors.append('configuration_version_invalid')
    for role in ROLES:
        image = value.get('images', {}).get(role, '')
        if not isinstance(image, str) or not re.fullmatch(r'[a-z0-9./_-]+@sha256:[a-f0-9]{64}', image):
            errors.append(role + '_image_digest_required')
        if value.get('image_sources', {}).get(role) != commit:
            errors.append(role + '_image_source_mismatch')
        inspection = value.get('image_inspections', {}).get(role, {})
        labels = inspection.get('Config', {}).get('Labels') or {}
        if (image not in inspection.get('RepoDigests', [])
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', inspection.get('Id', ''))
                or labels.get('org.opencontainers.image.revision') != commit):
            errors.append(role + '_pulled_image_provenance_unverified')
    return errors


def switch_plan(value: dict, *, rollback=False) -> dict:
    errors = validate_manifest(value)
    facts = value.get('observations', {})
    for name in ('remote_project_exists', 'ssh_host_key_verified', 'ssh_streamlocal_verified',
                 'socket_directory_verified', 'secrets_private_verified', 'api_read_only_probe_verified',
                 's3_tls_read_verified', 'live_orders_disabled'):
        if facts.get(name) is not True:
            errors.append(name + '_required')
    # Zero on both sides is mandatory before starting either target. No inferred drain.
    for name in ('local_collectors_running', 'local_executors_running', 'remote_collectors_running', 'remote_executors_running'):
        if type(facts.get(name)) is not int or facts[name] != 0:
            errors.append(name + '_must_be_zero')
    if facts.get('active_lease_handoff_verified') is not True:
        errors.append('active_lease_handoff_unverified')
    steps = (['stop_remote_workers', 'confirm_remote_workers_zero', 'keep_live_orders_disabled',
              'start_previous_local_workers', 'verify_checkpoint_and_config_consumption', 'lookup_unknown_orders_only']
             if rollback else ['freeze_configuration_and_images', 'stop_local_workers', 'confirm_local_workers_zero',
                               'confirm_checkpoint_lease_handoff', 'verify_ssh_and_remote_readonly_dependencies',
                               'start_remote_collector_and_executor_with_orders_disabled',
                               'verify_source_checkpoint_and_configuration_version', 'lookup_unknown_orders_only'])
    return {'eligible': not errors, 'blockers': sorted(set(errors)), 'direction': 'rollback' if rollback else 'remote',
            'source_commit': value.get('source_commit'), 'configuration_version': value.get('configuration_version'),
            'steps': steps, 'execution_performed': False, 'deletion_performed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(ROOT))
    parser.add_argument('--manifest')
    parser.add_argument('--rollback', action='store_true')
    parser.add_argument('--prepare', action='store_true')
    args = parser.parse_args()
    try:
        if args.prepare and not args.manifest:
            result=prepare(args.root,apply=True)
            print(json.dumps(result,sort_keys=True))
            return 0 if result['directory_ready'] else 1
        if not args.manifest:
            raise ValueError('manifest_required')
        source = project_path(args.root, args.manifest)
        value = json.loads(source.read_text())
        result = switch_plan(value, rollback=args.rollback)
        if args.prepare:
            result['preparation'] = prepare(args.root, apply=True)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['eligible'] else 1
    except Exception:
        print(json.dumps({'eligible': False, 'blockers': ['deployment_input_invalid'], 'execution_performed': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
