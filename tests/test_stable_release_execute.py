"""Offline publication contracts; all GitHub responses and approval custody are fixtures."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
import io
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from infra.android.release_policy import REQUIRED_SCOPES
from infra.release.stable_execute import (PublicationError, approved_bundle, assets_match,
    canonical, github_transport, publish, sha, verify_artifact_run, verify_external_signature,
    verify_pulled_images)
from infra.release.stable_plan import make_plan
from tests import test_stable_release_plan as plan_fixtures

REPO='fixture-owner/fixture-project'


class FakeGithub:
    def __init__(self,plan,files):
        self.plan=plan;self.files=files;self.calls=[];self.tag=False;self.rows=[]
        self.draft=True;self.release=False;self.fail=None;self.main_reads=0;self.drift=False
        self.protected=True;self.environment=True

    def __call__(self,method,route,data=None):
        self.calls.append((method,route,data))
        if self.fail and self.fail in route and method!='GET':return 502,{}
        suffix=route.removeprefix('/repos/'+REPO)
        if suffix=='':return 200,{'default_branch':'main','archived':False}
        if suffix=='/branches/main':
            self.main_reads+=1
            return 200,{'name':'main','protected':self.protected,'commit':{'sha':'a'*40 if self.drift and self.main_reads>1 else self.plan['source_commit']}}
        if suffix=='/environments/stable-release':
            return 200,{'protection_rules':[{'type':'required_reviewers','reviewers':[{'id':1}],'prevent_self_review':self.environment}],
                'deployment_branch_policy':{'protected_branches':True}}
        if suffix=='/git/ref/tags/'+self.plan['tag']:
            return (200,{'ref':'refs/tags/'+self.plan['tag'],'object':{'sha':self.plan['source_commit'],'type':'commit'}}) if self.tag else (404,{})
        if suffix=='/git/refs' and method=='POST':
            self.tag=True;return 201,{'ref':data['ref'],'object':{'sha':data['sha'],'type':'commit'}}
        if suffix=='/releases' and method=='POST':
            self.release=True;return 201,{'id':42,'draft':True,'prerelease':False,'tag_name':self.plan['tag']}
        if suffix.startswith('/releases/42/assets?name='):
            name=suffix.split('name=',1)[1]
            row={'name':name,'state':'uploaded','size':len(data),'digest':'sha256:'+sha(data)}
            self.rows.append(row);return 201,row
        if suffix=='/releases/42/assets?per_page=100':return 200,self.rows
        if suffix=='/releases/42' and method=='PATCH':
            self.draft=False;return 200,{'id':42,'draft':False,'prerelease':False}
        if suffix=='/releases/tags/'+self.plan['tag']:
            if not self.release:return 404,{}
            return 200,{'id':42,'tag_name':self.plan['tag'],'draft':self.draft,'prerelease':False,'assets':self.rows}
        raise AssertionError((method,route))


class StablePublicationTests(unittest.TestCase):
    def fixture(self,root):
        inputs=plan_fixtures.StableReleasePlanTests().fixture(root)
        for file in inputs['evidence'].glob('*.json'):
            row=json.loads(file.read_text());row.update(synthetic=False,fixture=False);file.write_text(json.dumps(row))
        plan=make_plan(**inputs)
        now=datetime.now(timezone.utc)
        manifest={json.loads(file.read_text())['scope']:sha(file.read_bytes()) for file in inputs['evidence'].glob('*.json')}
        payload={'format':'football-stable-approval-v1','repository':REPO,'source_commit':plan['source_commit'],
            'version':plan['version'],'plan_sha256':sha(canonical(plan)),'evidence_manifest_sha256':sha(canonical(manifest)),
            'real_environment':True,'synthetic':False,'approval_id':'fixture-approval','approved_by':'fixture-custodian',
            'issued_at':(now-timedelta(minutes=1)).isoformat(),'expires_at':(now+timedelta(minutes=10)).isoformat()}
        approval=root/'approval.json';approval.write_text(json.dumps({'payload':payload,'signature_ed25519':'fixture-only'}))
        return inputs,plan,approval,now

    def approved(self,root):
        inputs,plan,approval,now=self.fixture(root)
        # This patches custody, not evidence or source validation; no real signing key is created.
        with patch('infra.release.stable_execute.verify_external_signature') as signature:
            files,identifier=approved_bundle(plan,inputs,approval,'fixture-public',REPO,now=now)
        signature.assert_called_once()
        return plan,files,identifier

    def event(self,plan):
        return {'GITHUB_ACTIONS':'true','GITHUB_REF':'refs/heads/main','GITHUB_SHA':plan['source_commit'],
            'GITHUB_REPOSITORY':REPO,'STABLE_RELEASE_ENVIRONMENT':'stable-release'}

    def test_known_public_rfc8032_signature_and_modified_message(self):
        # RFC 8032 section 7.1 public test vector; no private key or signing operation.
        key=base64.b64encode(bytes.fromhex('d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a')).decode()
        signature=base64.b64encode(bytes.fromhex('e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b')).decode()
        verify_external_signature(key,signature,b'')
        with self.assertRaises(Exception):verify_external_signature(key,signature,b'modified')

    def test_bundle_requires_external_approval_even_schema_claims_real(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs,plan,approval,now=self.fixture(Path(directory))
            with self.assertRaisesRegex(PublicationError,'signature'):
                approved_bundle(plan,inputs,approval,'not-a-trusted-key',REPO,now=now)

    def test_evidence_archive_is_complete_deterministic_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);plan,files,_=self.approved(root)
            with zipfile.ZipFile(io.BytesIO(files['release-evidence.zip'])) as archive:
                self.assertEqual(9,len(archive.namelist()))
                self.assertIn('proof.log',archive.namelist())
                for scope in REQUIRED_SCOPES:self.assertIn('evidence/'+scope+'.json',archive.namelist())
            from infra.release.stable_execute import bounded_evidence_archive
            self.assertEqual(files['release-evidence.zip'],bounded_evidence_archive({'root':root,'evidence':root/'evidence'}))
            row=json.loads((root/'evidence/android_device.json').read_text());row['artifacts']*=17
            (root/'evidence/android_device.json').write_text(json.dumps(row))
            with self.assertRaisesRegex(PublicationError,'budget'):bounded_evidence_archive({'root':root,'evidence':root/'evidence'})

    def test_synthetic_fixture_missing_and_wrong_source_device_cannot_reach_custody(self):
        for kind in ('synthetic','fixture','missing_flag','missing_scope','wrong_source','wrong_device'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                inputs,plan,approval,now=self.fixture(Path(directory));file=inputs['evidence']/'android_device.json';row=json.loads(file.read_text())
                if kind=='missing_scope':file.unlink()
                else:
                    if kind=='synthetic':row['synthetic']=True
                    elif kind=='fixture':row['fixture']=True
                    elif kind=='missing_flag':row.pop('synthetic')
                    elif kind=='wrong_source':row['source_commit']='e'*40
                    else:row['apk_sha256']='e'*64
                    file.write_text(json.dumps(row))
                if kind in {'synthetic','fixture','missing_flag'}:plan=make_plan(**inputs)
                with patch('infra.release.stable_execute.verify_external_signature') as signature:
                    with self.assertRaises(ValueError):approved_bundle(plan,inputs,approval,'fixture',REPO,now=now)
                    signature.assert_not_called()

    def test_expired_wrong_plan_extra_fields_and_broken_signature_rejected(self):
        for kind in ('expired','future','too_long','plan','repository','extra','bool','signature'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                inputs,plan,approval,now=self.fixture(Path(directory));envelope=json.loads(approval.read_text());payload=envelope['payload']
                if kind=='expired':payload['expires_at']=(now-timedelta(seconds=1)).isoformat()
                elif kind=='future':payload['issued_at']=(now+timedelta(seconds=1)).isoformat()
                elif kind=='too_long':payload['expires_at']=(now+timedelta(days=2)).isoformat()
                elif kind=='plan':payload['plan_sha256']='e'*64
                elif kind=='repository':payload['repository']='other/repo'
                elif kind=='extra':payload['unexpected']='must never be republished'
                elif kind=='bool':payload['real_environment']=1
                approval.write_text(json.dumps(envelope))
                with patch('infra.release.stable_execute.verify_external_signature',side_effect=ValueError('fixture bad signature') if kind=='signature' else None):
                    with self.assertRaises(PublicationError):approved_bundle(plan,inputs,approval,'fixture',REPO,now=now)

    def test_happy_path_exact_tag_draft_all_assets_readback_then_publish_once(self):
        with tempfile.TemporaryDirectory() as directory:
            plan,files,approval=self.approved(Path(directory));api=FakeGithub(plan,files)
            result=publish(api,REPO,plan,files,approval,event=self.event(plan))
            self.assertTrue(result['published']);self.assertEqual(set(files),set(result['assets']))
            writes=[call for call in api.calls if call[0]!='GET']
            self.assertEqual('POST',writes[0][0]);self.assertEqual('/git/refs',writes[0][1].split(REPO)[1])
            self.assertEqual(1,sum(method=='PATCH' for method,_,_ in writes))
            self.assertFalse(any(method=='DELETE' for method,_,_ in api.calls))

    def test_wrong_workflow_unprotected_main_and_no_environment_block_before_writes(self):
        for kind in ('branch','sha','environment','unprotected','no_reviewer','existing_tag'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                plan,files,approval=self.approved(Path(directory));api=FakeGithub(plan,files);event=self.event(plan)
                if kind=='branch':event['GITHUB_REF']='refs/heads/feature'
                elif kind=='sha':event['GITHUB_SHA']='a'*40
                elif kind=='environment':event['STABLE_RELEASE_ENVIRONMENT']='none'
                elif kind=='unprotected':api.protected=False
                elif kind=='no_reviewer':api.environment=False
                else:api.tag=True
                with self.assertRaises(PublicationError):publish(api,REPO,plan,files,approval,event=event)
                self.assertFalse(any(method!='GET' for method,_,_ in api.calls))

    def test_upload_failure_or_main_drift_keeps_draft_without_retry_delete_or_publication(self):
        for kind in ('upload','drift'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                plan,files,approval=self.approved(Path(directory));api=FakeGithub(plan,files)
                if kind=='upload':api.fail='/assets?name='
                else:api.drift=True
                with self.assertRaises(PublicationError):publish(api,REPO,plan,files,approval,event=self.event(plan))
                self.assertTrue(api.draft);self.assertTrue(api.tag)
                self.assertFalse(any(method in {'PATCH','DELETE'} for method,_,_ in api.calls))
                if kind=='upload':self.assertEqual(1,sum(method=='UPLOAD' for method,_,_ in api.calls))

    def test_asset_digest_size_state_duplicates_and_missing_rejected(self):
        files={'fixture.apk':b'exact'};good={'name':'fixture.apk','state':'uploaded','size':5,'digest':'sha256:'+sha(b'exact')}
        assets_match([good],files)
        for key,value in [('digest','sha256:'+'0'*64),('size',4),('state','new'),('name','wrong')]:
            with self.subTest(key=key),self.assertRaises(PublicationError):assets_match([{**good,key:value}],files)
        for rows in ([],[good,good]):
            with self.assertRaises(PublicationError):assets_match(rows,files)

    def test_fresh_pulled_images_and_artifact_run_are_bound_to_source(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs,plan,_,_=self.fixture(Path(directory));images=[row['inspection'] for row in json.loads(inputs['image_inspect'].read_text()).values()]
            verify_pulled_images(plan,images)
            images[0]['Id']='sha256:'+'0'*64
            with self.assertRaises(PublicationError):verify_pulled_images(plan,images)
            run={'status':'completed','conclusion':'success','head_branch':'main','head_sha':plan['source_commit'],
                'repository':{'full_name':REPO},'event':'workflow_dispatch'}
            verify_artifact_run(plan,run,REPO)
            for key,value in [('head_sha','a'*40),('conclusion','failure'),('head_branch','feature'),('event','pull_request')]:
                with self.subTest(key=key),self.assertRaises(PublicationError):verify_artifact_run(plan,{**run,key:value},REPO)

    def test_transport_has_no_arbitrary_host_delete_or_force_ref_path(self):
        transport=github_transport('fixture-token',REPO)
        for method,route in [('DELETE','/repos/'+REPO+'/git/refs'),('PATCH','/repos/'+REPO+'/git/refs'),
            ('GET','https://example.invalid'),('POST','/repos/other/repo/releases'),('UPLOAD','/repos/'+REPO+'/branches/main')]:
            with self.subTest(method=method,route=route),self.assertRaises(PublicationError):transport(method,route)

    def test_execute_cli_missing_fresh_proofs_or_dirty_checkout_never_creates_transport(self):
        from infra.release.stable_execute import main
        for dirty in (False,True):
            with self.subTest(dirty=dirty),tempfile.TemporaryDirectory() as directory:
                inputs,plan,approval,_=self.fixture(Path(directory));root=inputs['root']
                plan_file=root/'plan.json';plan_file.write_text(json.dumps(plan))
                argv=['stable-execute','execute','--plan',str(plan_file),'--attestation',str(approval)]
                for key,value in inputs.items():argv.extend(['--'+key.replace('_','-'),str(value)])
                if dirty:
                    checkout=root/'current-worktree.txt';checkout.write_text(' M actual-source')
                    run=root/'run.json';run.write_text('{}')
                    pulled=root/'pulled.json';pulled.write_text('[]')
                    for key,value in [('actual-worktree-status',checkout),('artifact-run',run),('pulled-image-inspect',pulled),('actual-package-output',inputs['package_output'])]:
                        argv.extend(['--'+key,str(value)])
                stderr=io.StringIO()
                with patch('sys.argv',argv),patch.dict('os.environ',{'GITHUB_REPOSITORY':REPO,'STABLE_RELEASE_TRUSTED_PUBLIC_KEY':'fixture'}),\
                    patch('infra.release.stable_execute.verify_external_signature'),\
                    patch('infra.release.stable_execute.github_transport') as network,redirect_stderr(stderr),redirect_stdout(io.StringIO()):
                    self.assertEqual(1,main());network.assert_not_called()
                self.assertFalse(json.loads(stderr.getvalue())['published'])


if __name__=='__main__':unittest.main()
