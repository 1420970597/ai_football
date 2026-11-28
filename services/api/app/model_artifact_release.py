"""Explicit software prompt releases; never an inference-quality approval path."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re

from fastapi import HTTPException

from . import model_configuration as models
from .auth import audit

MAX_RELEASES = 20
COMMIT = re.compile(r'^[0-9a-f]{40}$')
RELEASE_FIELDS = {'release_id', 'source_commit', 'test_report_sha256', 'test_count', 'deployed_at', 'before', 'after'}


def validated_releases(state):
    releases = state.get('artifact_deployments', [])
    if not isinstance(releases, list) or len(releases) > MAX_RELEASES:
        raise ValueError('invalid prompt release history')
    seen = set()
    for release in releases:
        if (not isinstance(release, dict) or set(release) != RELEASE_FIELDS
                or not isinstance(release['release_id'], str) or not models.SAFE_VERSION.fullmatch(release['release_id'])
                or release['release_id'] in seen
                or not isinstance(release['source_commit'], str) or not COMMIT.fullmatch(release['source_commit'])
                or not isinstance(release['test_report_sha256'], str) or not models.HASH.fullmatch(release['test_report_sha256'])
                or type(release['test_count']) is not int or release['test_count'] < 1):
            raise ValueError('invalid prompt release evidence')
        stamp = datetime.fromisoformat(release['deployed_at'])
        if stamp.tzinfo is None:
            raise ValueError('prompt release time must be timezone-aware')
        seen.add(release['release_id'])
        before, after = release['before'], release['after']
        for snapshot in (before, after):
            if (not isinstance(snapshot, dict)
                    or snapshot.get('version') != models.digest({k: v for k, v in snapshot.items() if k != 'version'})
                    or type(snapshot.get('revision')) is not int or snapshot['revision'] < 1):
                raise ValueError('invalid prompt release snapshot')
            models._provenance(snapshot['provenance'])
        if (after['origin'] != 'deployed_prompt_artifact' or after.get('artifact_release_id') != release['release_id']
                or after['revision'] <= before['revision']
                or before['provenance']['prompt_sha256'] == after['provenance']['prompt_sha256']):
            raise ValueError('invalid prompt release transition')
        allowed = {'version', 'revision', 'origin', 'artifact_release_id', 'provenance'}
        if ({k: v for k, v in before.items() if k not in allowed}
                != {k: v for k, v in after.items() if k not in allowed}
                or {k: v for k, v in before['provenance'].items() if k != 'prompt_sha256'}
                != {k: v for k, v in after['provenance'].items() if k != 'prompt_sha256'}):
            raise ValueError('prompt release cannot change model or other artifacts')
    return releases


async def deploy_prompt_artifact(conn, settings, *, release_id, source_commit,
                                 test_report_sha256, test_count,
                                 expected_active_version, expected_prompt_sha256):
    """Caller owns the transaction and the idle handoff; no HTTP route exposes this."""
    if (not isinstance(release_id, str) or not models.SAFE_VERSION.fullmatch(release_id)
            or not isinstance(source_commit, str) or not COMMIT.fullmatch(source_commit)
            or not isinstance(test_report_sha256, str) or not models.HASH.fullmatch(test_report_sha256)
            or type(test_count) is not int or test_count < 1
            or not isinstance(expected_active_version, str) or not models.HASH.fullmatch(expected_active_version)
            or not isinstance(expected_prompt_sha256, str) or not models.HASH.fullmatch(expected_prompt_sha256)):
        raise HTTPException(422, '提示产物发布证据格式无效')
    await models.lock(conn)
    state = await models.read_state(conn, settings)
    releases = validated_releases(state)
    active = state['active']
    deployed_prompt = models.prompt_digest()
    previous = next((item for item in releases if item['release_id'] == release_id), None)
    if previous:
        if (previous['source_commit'] != source_commit or previous['test_report_sha256'] != test_report_sha256
                or previous['test_count'] != test_count or previous['before']['version'] != expected_active_version
                or previous['before']['provenance']['prompt_sha256'] != expected_prompt_sha256
                or previous['after'] != active or active['provenance']['prompt_sha256'] != deployed_prompt):
            raise HTTPException(409, '提示产物发布编号已使用且状态不一致')
        return {'changed': False, 'release_id': release_id, 'active_version': active['version']}
    if active['version'] != expected_active_version or active['provenance']['prompt_sha256'] != expected_prompt_sha256:
        raise HTTPException(409, '提示产物发布基线已变化')
    if deployed_prompt == expected_prompt_sha256:
        raise HTTPException(409, '提示产物未变化')
    if len(releases) >= MAX_RELEASES:
        raise HTTPException(413, '提示产物历史达到上限，不能删除既有证据')
    before = deepcopy(active)
    state['revision'] += 1
    after = {**deepcopy(active), 'revision': state['revision'], 'origin': 'deployed_prompt_artifact',
             'artifact_release_id': release_id,
             'provenance': {**active['provenance'], 'prompt_sha256': deployed_prompt}}
    after['version'] = models.digest({k: v for k, v in after.items() if k != 'version'})
    release = {'release_id': release_id, 'source_commit': source_commit,
               'test_report_sha256': test_report_sha256, 'test_count': test_count,
               'deployed_at': datetime.now(timezone.utc).isoformat(), 'before': before, 'after': deepcopy(after)}
    state['artifact_deployments'] = [*releases, release]
    state['active'] = after
    validated_releases(state)
    await models.save_state(conn, settings, state)
    await audit(conn, None, 'system', 'model_prompt_artifact_deployed', 'model_configuration', release_id,
                details={**release, 'quality_approval': False})
    return {'changed': True, 'release_id': release_id, 'active_version': after['version']}
