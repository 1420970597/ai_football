"""无网络开发候选发行策略；禁止创建、覆盖或复用旧资产。"""
import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from infra.release.development_policy import (
    DevelopmentPolicyError, _NoRedirects, github_reader, main, observe_candidate,
    select_candidate, verify_artifact_manifest,
)
from infra.release.manifest import make_manifest
import test_release_manifest as support

NOW = datetime(2026, 10, 27, 23, 58, tzinfo=timezone.utc)
COMMIT = 'a' * 40
TAG = 'v2.0.0-dev.20261027.1'
REPOSITORY = 'example/football'


def selected(event='push', *, tag=TAG, commit=COMMIT):
    return select_candidate(event, 'refs/tags/' + tag,
                            tag if event == 'workflow_dispatch' else '', commit, now=NOW)


class FakeGitHub:
    def __init__(self, *, tag=TAG, commit=COMMIT, release_status=404, objects=None):
        self.paths = []
        self.documents = {
            '/repos/' + REPOSITORY + '/git/ref/tags/' + tag:
                (200, {'ref': 'refs/tags/' + tag, 'object': {'type': 'commit', 'sha': commit}}),
            '/repos/' + REPOSITORY + '/releases/tags/' + tag: (release_status, {}),
        }
        self.documents.update(objects or {})

    def __call__(self, path):
        self.paths.append(path)
        return self.documents.get(path, (404, None))


class DevelopmentReleasePolicyTests(unittest.TestCase):
    def test_push_and_explicit_dispatch_create_distinct_nonproduction_candidate_names(self):
        first = selected()
        second = selected('workflow_dispatch', tag='v2.0.0-dev.20261027.2')
        self.assertNotEqual(first['candidate_tag'], second['candidate_tag'])
        for row in (first, second):
            self.assertEqual('2.0.0-dev', row['version'])
            self.assertEqual(COMMIT, row['source_commit'])
            self.assertTrue(row['prerelease'])
            self.assertFalse(row['production_ready'])
            self.assertFalse(row['published'])
            self.assertFalse(row['tag_created'])

    def test_missing_input_old_tag_wrong_ref_event_and_shell_payloads_are_rejected(self):
        bad_calls = [
            ('workflow_dispatch', 'refs/heads/main', '', COMMIT),
            ('workflow_dispatch', 'refs/heads/main', None, COMMIT),
            ('push', 'refs/heads/main', '', COMMIT),
            ('pull_request', 'refs/tags/' + TAG, '', COMMIT),
            ('push', 'refs/tags/' + TAG, 'v2.0.0-dev.20261027.2', COMMIT),
        ]
        for tag in ('v2.0.0-dev', 'v2.0.0', 'v2.0.0-dev.20261027.0',
                    'v2.0.0-dev.20261027.01', 'v2.0.0-dev.20261027.1000000',
                    TAG + '\n', TAG + ';touch /tmp/injected', '$(id)', '--latest',
                    'v2x0x0-devx20261027x1'):
            bad_calls.append(('workflow_dispatch', 'refs/heads/main', tag, COMMIT))
        for args in bad_calls:
            with self.subTest(args=args[:3]):
                with self.assertRaises(DevelopmentPolicyError):
                    select_candidate(*args, now=NOW)

    def test_full_sha_and_real_nonfuture_utc_date_are_required(self):
        for commit in ('a'*39, 'a'*41, 'A'*40, 'a'*40+'\n', 'refs/heads/main', None):
            with self.subTest(commit=commit), self.assertRaises(DevelopmentPolicyError):
                selected(commit=commit)
        for day in ('20260229', '20261301', '20261028'):
            with self.subTest(day=day), self.assertRaises(DevelopmentPolicyError):
                selected(tag='v2.0.0-dev.' + day + '.1')
        self.assertEqual('v2.0.0-dev.20240229.1', selected(tag='v2.0.0-dev.20240229.1')['candidate_tag'])

    def test_existing_lightweight_tag_must_equal_source_and_release_must_be_absent(self):
        reader = FakeGitHub()
        result = observe_candidate(selected(), REPOSITORY, reader)
        self.assertTrue(result['tag_verified'])
        self.assertTrue(result['release_absent'])
        self.assertEqual(COMMIT, result['tag_commit'])
        self.assertEqual(2, len(reader.paths))
        self.assertFalse(result['published'])
        for reader, message in ((FakeGitHub(commit='b'*40), 'source_commit_mismatch'),
                                (FakeGitHub(release_status=200), 'already_exists'),
                                (FakeGitHub(release_status=401), 'absence_not_verified'),
                                (FakeGitHub(release_status=503), 'absence_not_verified')):
            with self.subTest(message=message), self.assertRaisesRegex(DevelopmentPolicyError, message):
                observe_candidate(selected(), REPOSITORY, reader)

    def test_missing_tag_wrong_exact_ref_and_unknown_object_never_create_tag(self):
        for replacement in ((404, None), (403, None),
                (200, {'ref': 'refs/tags/other', 'object': {'type': 'commit', 'sha': COMMIT}}),
                (200, {'ref': 'refs/tags/' + TAG, 'object': {'type': 'tree', 'sha': COMMIT}})):
            reader = FakeGitHub()
            reader.documents['/repos/'+REPOSITORY+'/git/ref/tags/'+TAG] = replacement
            with self.subTest(replacement=replacement), self.assertRaises(DevelopmentPolicyError):
                observe_candidate(selected(), REPOSITORY, reader)
            self.assertEqual(1, len(reader.paths))

    def test_annotated_tag_chain_resolves_to_commit_and_cycles_or_depth_are_rejected(self):
        reader = FakeGitHub()
        root = '/repos/' + REPOSITORY
        reader.documents[root+'/git/ref/tags/'+TAG] = (200, {
            'ref': 'refs/tags/'+TAG, 'object': {'type': 'tag', 'sha': 'b'*40}})
        reader.documents[root+'/git/tags/'+'b'*40] = (200, {
            'sha': 'b'*40, 'object': {'type': 'tag', 'sha': 'c'*40}})
        reader.documents[root+'/git/tags/'+'c'*40] = (200, {
            'sha': 'c'*40, 'object': {'type': 'commit', 'sha': COMMIT}})
        self.assertEqual(2, observe_candidate(selected(), REPOSITORY, reader)['tag_objects_followed'])
        reader.documents[root+'/git/tags/'+'c'*40][1]['object'] = {'type': 'tag', 'sha': 'b'*40}
        with self.assertRaisesRegex(DevelopmentPolicyError, 'chain_invalid'):
            observe_candidate(selected(), REPOSITORY, reader)
        for index in range(1, 10):
            identity = f'{index:040x}'
            reader.documents[root+'/git/tags/'+identity] = (200, {
                'sha': identity, 'object': {'type': 'tag', 'sha': f'{index+1:040x}'}})
        reader.documents[root+'/git/ref/tags/'+TAG][1]['object'] = {'type': 'tag', 'sha': f'{1:040x}'}
        with self.assertRaisesRegex(DevelopmentPolicyError, 'chain_invalid'):
            observe_candidate(selected(), REPOSITORY, reader)

    def test_repository_path_cannot_escape_fixed_github_api_scope(self):
        for repository in ('', '../other', 'owner/repo/extra', 'owner/repo?x=y', 'https://evil.invalid/a'):
            reader = FakeGitHub()
            with self.subTest(repository=repository), self.assertRaises(DevelopmentPolicyError):
                observe_candidate(selected(), repository, reader)
            self.assertEqual([], reader.paths)

    def test_original_android_manifest_is_compatible_but_missing_images_or_wrong_source_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts, images = support.ReleaseManifestTests().fixture(root)
            manifest = make_manifest(root, COMMIT, images, artifacts)
            self.assertEqual(selected(), verify_artifact_manifest(selected(), manifest))
            self.assertEqual('2.0.0-dev', manifest['version'])
            for change in ({'source_commit': 'b'*40}, {'production_ready': True}, {'channel': 'production'},
                           {'images': {'api': images['api']}}, {'assets': {}}, {'version': '2.0.0'}):
                with self.subTest(change=change), self.assertRaises(DevelopmentPolicyError):
                    verify_artifact_manifest(selected(), {**manifest, **change})
            bad = copy.deepcopy(manifest)
            bad['assets']['ballnews-2.0.0-dev-debug.apk']['sha256'] = 'unknown'
            with self.assertRaises(DevelopmentPolicyError):
                verify_artifact_manifest(selected(), bad)

    def test_cli_outputs_exact_validated_tag_and_manifest_hash_without_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts, images = support.ReleaseManifestTests().fixture(root)
            manifest = root/'manifest.json'
            manifest.write_text(json.dumps(make_manifest(root, COMMIT, images, artifacts)))
            output, tag = root/'policy.json', root/'tag.txt'
            environment = {'RELEASE_EVENT': 'workflow_dispatch', 'RELEASE_REF': 'refs/heads/main',
                'CANDIDATE_TAG_INPUT': TAG, 'SOURCE_COMMIT': COMMIT, 'GITHUB_REPOSITORY': REPOSITORY,
                'GH_TOKEN': 'explicit-test-secret-not-a-real-token'}
            with patch.dict(os.environ, environment, clear=True), patch(
                    'infra.release.development_policy.github_reader', return_value=FakeGitHub()), patch(
                    'sys.argv', ['policy', '--output', str(output), '--tag-output', str(tag), '--manifest', str(manifest)]):
                main()
            result = json.loads(output.read_text())
            self.assertEqual(TAG+'\n', tag.read_text())
            self.assertEqual(64, len(result['release_manifest_sha256']))
            self.assertNotIn(environment['GH_TOKEN'], output.read_text())
            self.assertFalse(result['production_ready'])

    def test_http_errors_and_network_failures_remain_read_only_and_redacted(self):
        for token in ('', 'bad\nheader', 'bad\rheader'):
            with self.assertRaises(DevelopmentPolicyError):
                github_reader(token)
        reader = github_reader('test-only-token')
        with patch('infra.release.development_policy._read_response', side_effect=HTTPError(
                'https://api.github.com/redacted', 401, 'not printed', {}, None)):
            self.assertEqual((401, None), reader('/repos/example/football/releases/tags/'+TAG))
        with patch('infra.release.development_policy._read_response', side_effect=URLError('test-secret-should-not-print')):
            with self.assertRaisesRegex(DevelopmentPolicyError, '^github_read_failed$'):
                reader('/repos/example/football/git/ref/tags/'+TAG)
        with self.assertRaisesRegex(DevelopmentPolicyError, 'read_path_invalid'):
            reader('//other.invalid/private')
        with self.assertRaisesRegex(DevelopmentPolicyError, 'redirect_rejected'):
            _NoRedirects().redirect_request(None, None, 302, '', {}, 'https://other.invalid')

    def test_http_reader_checks_method_authentication_json_and_response_budget(self):
        class Response:
            status = 200
            headers = {}

            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, size):
                return self.body[:size]

        request = github_reader('explicit-test-token')
        with patch('infra.release.development_policy._read_response', return_value=Response(b'{"value":1}')) as response:
            self.assertEqual((200, {'value': 1}), request('/repos/example/football/git/ref/tags/'+TAG))
            sent = response.call_args[0][0]
            self.assertEqual('GET', sent.get_method())
            self.assertEqual('https://api.github.com/repos/example/football/git/ref/tags/'+TAG, sent.full_url)
            self.assertEqual('Bearer explicit-test-token', sent.get_header('Authorization'))
        for body, category in ((b'x'*(1024*1024+1), 'response_budget_exceeded'),
                               (b'not-json', 'read_failed')):
            with patch('infra.release.development_policy._read_response', return_value=Response(body)):
                with self.assertRaisesRegex(DevelopmentPolicyError, category):
                    request('/repos/example/football/releases/tags/'+TAG)

    def test_cli_missing_input_fails_before_network_and_writes_no_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output, tag = Path(temporary)/'policy.json', Path(temporary)/'tag.txt'
            with patch.dict(os.environ, {'RELEASE_EVENT': 'workflow_dispatch', 'SOURCE_COMMIT': COMMIT}, clear=True), patch(
                    'infra.release.development_policy.github_reader') as reader, patch('sys.stderr', new_callable=io.StringIO) as stderr, patch(
                    'sys.argv', ['policy', '--output', str(output), '--tag-output', str(tag)]):
                with self.assertRaises(SystemExit) as failure:
                    main()
                self.assertEqual(1, failure.exception.code)
                self.assertEqual('dispatch_requires_explicit_candidate_tag\n', stderr.getvalue())
                reader.assert_not_called()
            self.assertFalse(output.exists())
            self.assertFalse(tag.exists())

    def test_workflow_runs_policy_full_verify_and_recheck_without_input_shell_interpolation(self):
        workflow = (Path(__file__).resolve().parents[1]/'.github/workflows/development-release.yml').read_text()
        self.assertIn("tags: ['v2.0.0-dev.*']", workflow)
        self.assertIn('required: true', workflow)
        self.assertIn('needs: policy\n    uses: ./.github/workflows/verify.yml', workflow)
        self.assertIn('needs: [policy, verify]', workflow)
        self.assertEqual(2, workflow.count('python infra/release/development_policy.py --output'))
        self.assertIn('--verify-tag --target "$SOURCE_COMMIT" --prerelease', workflow)
        self.assertIn('SOURCE_COMMIT: $'+'{{ github.sha }}', workflow)
        self.assertNotIn('gh release create v2.0.0-dev ', workflow)
        self.assertNotIn('gh release delete', workflow)
        self.assertNotIn('git tag', workflow)
        in_shell = False
        for line in workflow.splitlines():
            if line.lstrip().startswith('run: |'):
                in_shell = True
                continue
            if in_shell and line.strip() and not line.startswith('          '):
                in_shell = False
            if in_shell:
                self.assertNotIn('$'+'{{', line)
