"""不可变开发候选标签策略；远端只读核验，不创建或覆盖任何发行。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from infra.release.manifest import COMPONENT_SERVICES, DEVELOPMENT_VERSION, compose_override

TAG_PATTERN = r'v2[.]0[.]0-dev[.]([0-9]{8})[.]([1-9][0-9]{0,5})'
MAX_TAG_OBJECTS = 8


class DevelopmentPolicyError(ValueError):
    pass


def full_commit(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}', value):
        raise DevelopmentPolicyError('source_commit_must_be_full_40_hex')
    return value


def select_candidate(event_name, ref, input_tag, source_commit, *, now=None):
    source_commit = full_commit(source_commit)
    if event_name == 'workflow_dispatch':
        if not isinstance(input_tag, str) or not input_tag:
            raise DevelopmentPolicyError('dispatch_requires_explicit_candidate_tag')
        tag = input_tag
    elif event_name == 'push':
        if not isinstance(ref, str) or not ref.startswith('refs/tags/'):
            raise DevelopmentPolicyError('push_requires_tag_ref')
        tag = ref[len('refs/tags/'):]
        if input_tag not in (None, '', tag):
            raise DevelopmentPolicyError('push_input_does_not_match_tag_ref')
    else:
        raise DevelopmentPolicyError('unsupported_release_event')
    matched = re.fullmatch(TAG_PATTERN, tag)
    if not matched:
        raise DevelopmentPolicyError('candidate_tag_format_invalid')
    try:
        tag_day = datetime.strptime(matched[1], '%Y%m%d').date()
    except ValueError:
        raise DevelopmentPolicyError('candidate_tag_utc_date_invalid') from None
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    if tag_day > today:
        raise DevelopmentPolicyError('candidate_tag_utc_date_is_future')
    return {'format': 'development-candidate-policy-1', 'candidate_tag': tag,
            'source_commit': source_commit, 'version': DEVELOPMENT_VERSION,
            'channel': 'development', 'prerelease': True, 'production_ready': False,
            'published': False, 'tag_created': False}


def observe_candidate(selection, repository, request):
    if not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}', repository):
        raise DevelopmentPolicyError('repository_identifier_invalid')
    tag = selection['candidate_tag']
    prefix = '/repos/' + repository
    status, reference = request(prefix + '/git/ref/tags/' + tag)
    if status != 200 or not isinstance(reference, dict) or reference.get('ref') != 'refs/tags/' + tag:
        raise DevelopmentPolicyError('existing_exact_candidate_tag_required')
    obj = reference.get('object')
    seen = set()
    for depth in range(MAX_TAG_OBJECTS + 1):
        if not isinstance(obj, dict) or obj.get('type') not in {'tag', 'commit'}:
            raise DevelopmentPolicyError('candidate_tag_object_invalid')
        sha = full_commit(obj.get('sha'))
        if obj['type'] == 'commit':
            break
        if sha in seen or depth == MAX_TAG_OBJECTS:
            raise DevelopmentPolicyError('candidate_tag_chain_invalid')
        seen.add(sha)
        status, document = request(prefix + '/git/tags/' + sha)
        if status != 200 or not isinstance(document, dict) or document.get('sha') != sha:
            raise DevelopmentPolicyError('candidate_tag_object_unreadable')
        obj = document.get('object')
    if sha != selection['source_commit']:
        raise DevelopmentPolicyError('candidate_tag_source_commit_mismatch')
    status, _ = request(prefix + '/releases/tags/' + tag)
    if status == 200:
        raise DevelopmentPolicyError('candidate_release_already_exists')
    if status != 404:
        raise DevelopmentPolicyError('candidate_release_absence_not_verified')
    return {**selection, 'tag_commit': sha, 'tag_verified': True,
            'release_absent': True, 'tag_objects_followed': len(seen)}


def verify_artifact_manifest(policy, manifest):
    if (manifest.get('source_commit') != policy['source_commit']
            or manifest.get('version') != DEVELOPMENT_VERSION
            or manifest.get('channel') != 'development' or manifest.get('production_ready') is not False
            or set(manifest.get('images', {})) != set(COMPONENT_SERVICES)):
        raise DevelopmentPolicyError('candidate_artifact_provenance_mismatch')
    try:
        compose_override(manifest)
    except (TypeError, ValueError):
        raise DevelopmentPolicyError('candidate_image_provenance_invalid') from None
    if set(manifest.get('assets', {})) != {
            'ballnews-2.0.0-dev-debug.apk', 'ballnews-2.0.0-dev-unsigned.apk'}:
        raise DevelopmentPolicyError('candidate_apk_assets_incomplete')
    for kind in ('debug', 'unsigned'):
        asset = manifest['assets']['ballnews-2.0.0-dev-' + kind + '.apk']
        if (asset.get('signing') != kind or type(asset.get('bytes')) is not int or asset['bytes'] <= 0
                or not re.fullmatch('[0-9a-f]{64}', asset.get('sha256', ''))):
            raise DevelopmentPolicyError('candidate_apk_evidence_invalid')
    return policy


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise DevelopmentPolicyError('github_redirect_rejected')


def _read_response(request):
    return build_opener(_NoRedirects()).open(request, timeout=10)


def github_reader(token):
    if not token or chr(10) in token or chr(13) in token:
        raise DevelopmentPolicyError('github_read_token_required')

    def read(path):
        if not re.fullmatch(r'/repos/[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}/(?:git/ref/tags/v2[.]0[.]0-dev[.][0-9]{8}[.][1-9][0-9]{0,5}|git/tags/[0-9a-f]{40}|releases/tags/v2[.]0[.]0-dev[.][0-9]{8}[.][1-9][0-9]{0,5})', path):
            raise DevelopmentPolicyError('github_read_path_invalid')
        request = Request('https://api.github.com' + path, headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'platform-development-release-policy'})
        try:
            with _read_response(request) as response:
                if response.headers.get('Content-Length') and int(response.headers['Content-Length']) > 1024*1024:
                    raise DevelopmentPolicyError('github_response_budget_exceeded')
                body = response.read(1024*1024 + 1)
                if len(body) > 1024*1024:
                    raise DevelopmentPolicyError('github_response_budget_exceeded')
                return response.status, json.loads(body)
        except HTTPError as error:
            # No URLs, bodies, request headers or credentials leave this helper.
            return error.code, None
        except DevelopmentPolicyError:
            raise
        except (URLError, TimeoutError, ValueError):
            raise DevelopmentPolicyError('github_read_failed') from None
    return read


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tag-output', type=Path, required=True)
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args()
    try:
        selection = select_candidate(os.environ.get('RELEASE_EVENT'), os.environ.get('RELEASE_REF'),
            os.environ.get('CANDIDATE_TAG_INPUT'), os.environ.get('SOURCE_COMMIT'))
        observed = observe_candidate(selection, os.environ.get('GITHUB_REPOSITORY'),
                                     github_reader(os.environ.get('GH_TOKEN')))
        if args.manifest:
            raw = args.manifest.read_bytes()
            verify_artifact_manifest(observed, json.loads(raw))
            observed['release_manifest_sha256'] = hashlib.sha256(raw).hexdigest()
        args.output.write_text(json.dumps(observed, ensure_ascii=False, indent=2) + chr(10))
        args.tag_output.write_text(observed['candidate_tag'] + chr(10))
    except (DevelopmentPolicyError, OSError, json.JSONDecodeError) as error:
        # Errors intentionally contain policy categories only, never raw network
        # exceptions, environment values, remote response bodies or secrets.
        message = str(error) if isinstance(error, DevelopmentPolicyError) else 'candidate_policy_io_or_json_failed'
        print(message, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
