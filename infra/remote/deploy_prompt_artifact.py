"""Run explicitly inside the verified image while the review handoff gate is held."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import asyncpg

from app.config import load_settings
from app.model_artifact_release import deploy_prompt_artifact


def verified_report(report_path, source_commit, app_root=Path('/app')):
    raw = Path(report_path).read_bytes()
    report = json.loads(raw)
    if (report.get('source_parent_commit') != source_commit
            or report.get('committed_source_verified') is not True
            or type(report.get('tests')) is not int or report['tests'] < 1
            or any(type(report.get(key)) is not int or report[key] != 0 for key in ('failures', 'errors', 'skipped'))):
        raise ValueError('frozen source report verification failed')
    hashes = report.get('source_sha256')
    if not isinstance(hashes, dict):
        raise ValueError('frozen source hashes missing')
    for directory in ('app', 'migrations'):
        files = {p.relative_to(app_root).as_posix(): p for p in (app_root / directory).rglob('*')
                 if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
        expected = {k.removeprefix('services/api/'): v for k, v in hashes.items()
                    if k.startswith('services/api/' + directory + '/')}
        if not files or files.keys() != expected.keys():
            raise ValueError('deployed file set differs from frozen report')
        for name, path in files.items():
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected[name]:
                raise ValueError('deployed artifact differs from frozen report')
    if (hashlib.sha256((app_root / 'requirements.txt').read_bytes()).hexdigest()
            != hashes.get('services/api/requirements.txt')):
        raise ValueError('deployed requirements differ from frozen report')
    return raw, report


async def run(args):
    raw, report = verified_report(args.report, args.source_commit)
    settings = load_settings(role='worker')
    conn = await asyncpg.connect(settings.database_url, timeout=5, command_timeout=5,
        server_settings={'application_name': 'explicit-prompt-artifact-release', 'statement_timeout': '5000'})
    try:
        async with asyncio.timeout(30), conn.transaction():
            result = await deploy_prompt_artifact(conn, settings, release_id=args.release_id,
                source_commit=args.source_commit, test_report_sha256=hashlib.sha256(raw).hexdigest(),
                test_count=report['tests'], expected_active_version=args.expected_active_version,
                expected_prompt_sha256=args.expected_prompt_sha256)
        print(json.dumps({**result, 'test_count': report['tests'], 'model_and_provider_unchanged': True,
                          'quality_approval': False}))
    finally:
        await conn.close(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ('report', 'source-commit', 'release-id', 'expected-active-version', 'expected-prompt-sha256'):
        parser.add_argument('--' + field, required=True)
    try:
        asyncio.run(run(parser.parse_args()))
    except Exception as exc:
        print(json.dumps({'event': 'prompt_artifact_release_failed', 'category': type(exc).__name__}))
        raise SystemExit(1) from None
