"""Externally approved immutable stable publication; no build, signing or key creation."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import zipfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from infra.android.device_artifact import local_file
from infra.android.release_policy import REQUIRED_SCOPES
from infra.release.stable_plan import validate_plan

MAX_JSON = 4 * 1024 * 1024
MAX_ASSET = 64 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
REPOSITORY = re.compile(r'[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}\Z')


class PublicationError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def read_bounded(root, path, limit=MAX_JSON):
    file = local_file(root, path)
    if not 0 < file.stat().st_size <= limit:
        raise PublicationError('release_input_budget_exceeded')
    return file.read_bytes()


def verify_external_signature(public_key, signature, message):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
    key.verify(base64.b64decode(signature, validate=True), message)


def bounded_evidence_archive(inputs):
    root=inputs['root'].resolve()
    directory=inputs['evidence'].resolve()
    if root not in directory.parents or directory.is_symlink():
        raise PublicationError('evidence_directory_invalid')
    records=sorted(directory.glob('*.json'))
    if not 1 <= len(records) <= 32:
        raise PublicationError('evidence_record_budget_exceeded')
    files={};total=0
    for record_file in records:
        raw=read_bounded(root,record_file,256*1024)
        record=json.loads(raw)
        if not isinstance(record,dict):raise PublicationError('evidence_record_invalid')
        if record.get('scope') not in REQUIRED_SCOPES:continue
        artifacts=record.get('artifacts')
        if not isinstance(artifacts,list) or not 1 <= len(artifacts) <= 16:
            raise PublicationError('evidence_artifact_budget_exceeded')
        files[str(record_file.relative_to(root))]=raw
        for artifact in artifacts:
            path=local_file(root,Path(artifact['path']))
            files[str(path.relative_to(root))]=read_bounded(root,path,16*1024*1024)
    total=sum(map(len,files.values()))
    if len(files)>64 or total>32*1024*1024:
        raise PublicationError('evidence_total_budget_exceeded')
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for name,raw in sorted(files.items()):
            info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100644 << 16;archive.writestr(info,raw)
    return stream.getvalue()


def approved_bundle(plan, inputs, attestation, trusted_public_key, repository, *, now=None):
    """Schema claims alone never authorize publication: require external Ed25519 custody."""
    for key in ('manifest','package_output','image_inspect','notes'):
        read_bounded(inputs['root'],inputs[key])
    if local_file(inputs['root'],inputs['apk']).stat().st_size>MAX_ASSET:
        raise PublicationError('release_asset_budget_exceeded')
    evidence_archive=bounded_evidence_archive(inputs)
    validate_plan(plan, **inputs)
    if not REPOSITORY.fullmatch(repository):
        raise PublicationError('repository_invalid')
    evidence = inputs['evidence']
    evidence_manifest = {}
    for file in sorted(evidence.glob('*.json')):
        record = json.loads(read_bounded(inputs['root'], file))
        if record.get('scope') not in REQUIRED_SCOPES:
            continue
        if record.get('synthetic') is not False or record.get('fixture') is not False:
            raise PublicationError('real_evidence_must_explicitly_exclude_synthetic_fixtures')
        evidence_manifest[record['scope']] = sha(file.read_bytes())
    if set(evidence_manifest) != REQUIRED_SCOPES:
        raise PublicationError('real_evidence_scopes_incomplete')
    envelope = json.loads(read_bounded(inputs['root'], attestation, 16384))
    if set(envelope) != {'payload', 'signature_ed25519'} or not isinstance(envelope['payload'], dict):
        raise PublicationError('external_attestation_invalid')
    payload = envelope['payload']
    expected = {'format': 'football-stable-approval-v1', 'repository': repository,
        'source_commit': plan['source_commit'], 'version': plan['version'],
        'plan_sha256': sha(canonical(plan)), 'evidence_manifest_sha256': sha(canonical(evidence_manifest)),
        'real_environment': True, 'synthetic': False}
    if (set(payload) != set(expected) | {'approval_id','approved_by','issued_at','expires_at'}
            or any(type(payload.get(key)) is not type(value) or payload.get(key) != value for key, value in expected.items())):
        raise PublicationError('external_attestation_scope_mismatch')
    if (not re.fullmatch(r'[A-Za-z0-9_.:-]{3,160}', str(payload.get('approval_id', '')))
            or not re.fullmatch(r'[A-Za-z0-9_.@:-]{3,160}', str(payload.get('approved_by', '')))):
        raise PublicationError('external_approval_identity_missing')
    try:
        start = datetime.fromisoformat(payload['issued_at'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(payload['expires_at'].replace('Z', '+00:00'))
        current = now or datetime.now(timezone.utc)
        if start.utcoffset() is None or end.utcoffset() is None or not start <= current <= end or (end-start).total_seconds() > 86400:
            raise ValueError
        verify_external_signature(trusted_public_key, envelope['signature_ed25519'], canonical(payload))
    except Exception:
        raise PublicationError('external_approval_signature_or_time_invalid') from None
    files = {'release-plan.json': canonical(plan), 'compose-images.json': canonical(plan['compose_override']),
        'release-approval.json': canonical(envelope), 'release-evidence.json': canonical(evidence_manifest),
        'release-evidence.zip':evidence_archive,
        'release-notes.md': read_bounded(inputs['root'], inputs['notes']),
        'signing-manifest.json': read_bounded(inputs['root'], inputs['manifest']),
        'package-verification.txt': read_bounded(inputs['root'], inputs['package_output']),
        'ballnews-'+plan['version']+'-signed.apk': read_bounded(inputs['root'], inputs['apk'], MAX_ASSET)}
    if any(not 0 < len(value) <= MAX_ASSET for value in files.values()) or sum(map(len, files.values())) > MAX_TOTAL:
        raise PublicationError('release_asset_budget_exceeded')
    return files, payload['approval_id']


def verify_pulled_images(plan, inspections):
    if not isinstance(inspections, list) or len(inspections) != len(plan['images']):
        raise PublicationError('fresh_image_inspection_incomplete')
    for component, reference in plan['images'].items():
        matches = [row for row in inspections if reference in row.get('RepoDigests', [])]
        if len(matches) != 1:
            raise PublicationError('fresh_image_digest_missing_or_duplicated')
        row = matches[0]
        labels = row.get('Config', {}).get('Labels') or {}
        if (row.get('Id') != plan['image_provenance'][component]['local_image_id']
                or labels.get('org.opencontainers.image.revision') != plan['source_commit']
                or labels.get('org.opencontainers.image.version') != plan['version']):
            raise PublicationError('fresh_image_identity_mismatch')


def verify_artifact_run(plan, record, repository):
    if (record.get('status') != 'completed' or record.get('conclusion') != 'success'
            or record.get('head_branch') != 'main' or record.get('head_sha') != plan['source_commit']
            or record.get('repository', {}).get('full_name') != repository
            or record.get('event') not in {'workflow_dispatch','workflow_run','push'}):
        raise PublicationError('evidence_artifact_run_must_be_successful_exact_main')


def remote_preflight(request, repository, plan):
    prefix = '/repos/'+repository
    status, repo = request('GET', prefix)
    if status != 200 or repo.get('default_branch') != 'main' or repo.get('archived') is not False:
        raise PublicationError('repository_default_main_required')
    status, branch = request('GET', prefix+'/branches/main')
    if (status != 200 or branch.get('name') != 'main' or branch.get('protected') is not True
            or branch.get('commit', {}).get('sha') != plan['source_commit']):
        raise PublicationError('protected_main_exact_source_required')
    status, environment = request('GET', prefix+'/environments/stable-release')
    rules = environment.get('protection_rules', []) if isinstance(environment, dict) else []
    required = [item for item in rules if item.get('type') == 'required_reviewers' and item.get('reviewers') and item.get('prevent_self_review') is True]
    if (status != 200 or not required
            or environment.get('deployment_branch_policy', {}).get('protected_branches') is not True):
        raise PublicationError('protected_environment_review_required')
    for suffix in ('/git/ref/tags/'+plan['tag'], '/releases/tags/'+plan['tag']):
        status, _ = request('GET', prefix+suffix)
        if status != 404:
            raise PublicationError('immutable_tag_and_release_must_be_absent')


def assets_match(rows, files):
    if not isinstance(rows, list) or len(rows) != len(files):
        raise PublicationError('release_assets_incomplete')
    seen = set()
    for row in rows:
        name = row.get('name')
        if (name not in files or name in seen or row.get('state') != 'uploaded'
                or row.get('size') != len(files[name]) or row.get('digest') != 'sha256:'+sha(files[name])):
            raise PublicationError('release_asset_readback_mismatch')
        seen.add(name)


def publish(request, repository, plan, files, approval_id, *, event):
    if (event.get('GITHUB_ACTIONS') != 'true' or event.get('GITHUB_REF') != 'refs/heads/main'
            or event.get('GITHUB_SHA') != plan['source_commit'] or event.get('GITHUB_REPOSITORY') != repository
            or event.get('STABLE_RELEASE_ENVIRONMENT') != 'stable-release'):
        raise PublicationError('protected_main_workflow_required')
    remote_preflight(request, repository, plan)
    prefix = '/repos/'+repository
    status, ref = request('POST', prefix+'/git/refs', {'ref':'refs/tags/'+plan['tag'], 'sha':plan['source_commit']})
    if (status != 201 or ref.get('ref') != 'refs/tags/'+plan['tag']
            or ref.get('object', {}).get('sha') != plan['source_commit'] or ref.get('object', {}).get('type') != 'commit'):
        raise PublicationError('tag_create_failed_no_retry')
    status, release = request('POST', prefix+'/releases', {'tag_name':plan['tag'], 'target_commitish':plan['source_commit'],
        'name':plan['tag'], 'body':files['release-notes.md'].decode(), 'draft':True, 'prerelease':False,
        'generate_release_notes':False})
    if (status != 201 or type(release.get('id')) is not int or release.get('draft') is not True
            or release.get('tag_name') != plan['tag'] or release.get('prerelease') is not False):
        raise PublicationError('draft_create_failed_no_retry')
    identifier = release['id']
    for name, value in files.items():
        status, row = request('UPLOAD', prefix+'/releases/'+str(identifier)+'/assets?name='+quote(name, safe=''), value)
        if status != 201:
            raise PublicationError('asset_upload_failed_draft_preserved_no_retry')
        assets_match([row], {name:value})
    status, rows = request('GET', prefix+'/releases/'+str(identifier)+'/assets?per_page=100')
    if status != 200:
        raise PublicationError('draft_assets_unreadable')
    assets_match(rows, files)
    # Recheck main and the exact created tag before the single draft->published change.
    status, branch = request('GET', prefix+'/branches/main')
    status_ref, ref = request('GET', prefix+'/git/ref/tags/'+plan['tag'])
    if (status != 200 or branch.get('protected') is not True or branch.get('commit', {}).get('sha') != plan['source_commit']
            or status_ref != 200 or ref.get('object', {}).get('sha') != plan['source_commit'] or ref.get('object', {}).get('type') != 'commit'):
        raise PublicationError('source_changed_draft_preserved')
    status, final = request('PATCH', prefix+'/releases/'+str(identifier), {'draft':False, 'make_latest':'true'})
    if status != 200 or final.get('draft') is not False or final.get('prerelease') is not False:
        raise PublicationError('publication_result_unknown_no_retry')
    status, final = request('GET', prefix+'/releases/tags/'+plan['tag'])
    if (status != 200 or final.get('id') != identifier or final.get('draft') is not False
            or final.get('prerelease') is not False or final.get('tag_name') != plan['tag']):
        raise PublicationError('publication_readback_failed_no_retry')
    assets_match(final.get('assets'), files)
    return {'format':'football-stable-publication-v1','published':True,'tag_created':True,'release_id':identifier,
        'tag':plan['tag'],'source_commit':plan['source_commit'],'approval_id':approval_id,
        'assets':{name:{'bytes':len(value),'sha256':sha(value)} for name,value in files.items()}}


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise PublicationError('github_redirect_rejected')


def github_transport(token, repository):
    if not token or any(char in token for char in '\r\n') or not REPOSITORY.fullmatch(repository):
        raise PublicationError('github_token_or_repository_invalid')
    prefix='/repos/'+repository
    paths={'GET':(r'',r'/branches/main',r'/environments/stable-release',r'/git/ref/tags/v[0-9]+[.][0-9]+[.][0-9]+',
        r'/releases/tags/v[0-9]+[.][0-9]+[.][0-9]+',r'/releases/[0-9]+/assets[?]per_page=100'),
        'POST':(r'/git/refs',r'/releases'),'PATCH':(r'/releases/[0-9]+',),
        'UPLOAD':(r'/releases/[0-9]+/assets[?]name=[A-Za-z0-9._%-]+',)}
    def request(method, route, data=None):
        if not any(re.fullmatch(re.escape(prefix)+suffix,route) for suffix in paths.get(method,())):
            raise PublicationError('github_method_or_path_rejected')
        upload=method=='UPLOAD'
        body=data if upload else canonical(data) if data is not None else None
        if body is not None and len(body) > (MAX_ASSET if upload else MAX_JSON):
            raise PublicationError('github_request_budget_exceeded')
        req=Request(('https://uploads.github.com' if upload else 'https://api.github.com')+route,
            method='POST' if upload else method,data=body,headers={'Authorization':'Bearer '+token,
                'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28',
                'Content-Type':'application/octet-stream' if upload else 'application/json','User-Agent':'football-stable-release'})
        try:
            with build_opener(NoRedirects()).open(req,timeout=30 if upload else 10) as response:
                raw=response.read(MAX_JSON+1)
                if len(raw)>MAX_JSON:raise PublicationError('github_response_budget_exceeded')
                return response.status,json.loads(raw)
        except HTTPError as error:
            return error.code,{}
        except PublicationError:raise
        except (OSError,URLError,ValueError):
            raise PublicationError('github_io_failed_no_retry') from None
    return request


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['validate','image-list','execute'])
    parser.add_argument('--root',type=Path,default=Path.cwd())
    for name in ('plan','manifest','apk','package-output','worktree-status','image-inspect','evidence','notes','attestation'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--actual-package-output',type=Path)
    parser.add_argument('--pulled-image-inspect',type=Path)
    parser.add_argument('--artifact-run',type=Path)
    parser.add_argument('--actual-worktree-status',type=Path)
    args=parser.parse_args()
    try:
        inputs={key:getattr(args,key) for key in ('root','manifest','apk','package_output','worktree_status','image_inspect','evidence','notes')}
        plan=json.loads(read_bounded(args.root,args.plan))
        files,approval=approved_bundle(plan,inputs,args.attestation,os.environ.get('STABLE_RELEASE_TRUSTED_PUBLIC_KEY',''),os.environ.get('GITHUB_REPOSITORY',''))
        result={'publication_state':'externally_approved_validated','published':False,'source_commit':plan['source_commit']}
        if args.action=='image-list':
            print('\n'.join(plan['images'].values()))
            return 0
        if args.action=='execute':
            if not all((args.actual_package_output,args.pulled_image_inspect,args.artifact_run,args.actual_worktree_status)):
                raise PublicationError('fresh_apk_and_image_verification_required')
            if local_file(args.root,args.actual_worktree_status).read_text().strip():
                raise PublicationError('current_source_must_remain_clean')
            verify_artifact_run(plan,json.loads(read_bounded(args.root,args.artifact_run)),os.environ.get('GITHUB_REPOSITORY',''))
            from infra.android.device_artifact import verify_device_artifact
            actual=verify_device_artifact(args.root,args.manifest,args.apk,args.actual_package_output)
            if actual.evidence()!=plan['apk']:
                raise PublicationError('fresh_apk_identity_mismatch')
            verify_pulled_images(plan,json.loads(read_bounded(args.root,args.pulled_image_inspect)))
            result=publish(github_transport(os.environ.get('GH_TOKEN',''),os.environ.get('GITHUB_REPOSITORY','')),
                os.environ['GITHUB_REPOSITORY'],plan,files,approval,event=os.environ)
        print(json.dumps(result,sort_keys=True))
        return 0
    except Exception as error:
        category=str(error) if isinstance(error,PublicationError) else 'stable_inputs_or_evidence_rejected'
        print(json.dumps({'published':False,'status':'rejected','reason':category}),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
