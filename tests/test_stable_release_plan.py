"""A stable dry run validates synthetic local inputs and never contacts GitHub."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from infra.android.release_policy import REQUIRED_SCOPES
from infra.release.manifest import COMPONENT_SERVICES
from infra.release.stable_plan import CONFIGURATION_PATHS, main, make_plan, validate_plan
from tests.android import test_device_contract as device_fixtures


class StableReleasePlanTests(unittest.TestCase):
    def fixture(self, root):
        manifest, apk, package = device_fixtures.DeviceContractTests().artifact(root, version='2.0.0')
        record=json.loads(manifest.read_text());record['source_clean']=True;manifest.write_text(json.dumps(record))
        status=root/'worktree.txt';status.write_text('')
        notes=root/'notes.md';notes.write_text('Explicit synthetic release notes; no remote publication.')
        for name in CONFIGURATION_PATHS:
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('explicit synthetic contract')
        migration=root/'services/api/migrations/001_fixture.sql';migration.parent.mkdir(parents=True);migration.write_text('-- isolated fixture')
        images={}
        for component in COMPONENT_SERVICES:
            reference=f'ghcr.io/example/ai-football-{component}@sha256:'+'c'*64
            images[component]={'reference':reference,'inspection':{'Id':'sha256:'+'d'*64,'RepoDigests':[reference],
                'Config':{'Labels':{'org.opencontainers.image.revision':'b'*40,'org.opencontainers.image.version':'2.0.0'}}}}
        inspect=root/'images.json';inspect.write_text(json.dumps(images))
        evidence=root/'evidence';evidence.mkdir()
        proof=root/'proof.log';proof.write_text('explicit synthetic fixture, never real production proof')
        for scope in REQUIRED_SCOPES:
            row={'scope':scope,'status':'passed','exit_code':0,'source_commit':'b'*40,'version':'2.0.0',
                 'real_environment':True,'checked_at':datetime.now(timezone.utc).isoformat(),
                 'command':'explicit synthetic test','apk_sha256':record['apk_sha256'],
                 'artifacts':[{'path':'proof.log','sha256':hashlib.sha256(proof.read_bytes()).hexdigest()}]}
            (evidence/f'{scope}.json').write_text(json.dumps(row))
        return {'root':root,'manifest':manifest,'apk':apk,'package_output':package,'worktree_status':status,
                'image_inspect':inspect,'evidence':evidence,'notes':notes}

    def test_complete_plan_links_signed_apk_six_images_config_and_migrations_without_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs=self.fixture(Path(directory));plan=make_plan(**inputs)
            self.assertEqual('validated_dry_run',plan['publication_state']);self.assertFalse(plan['published'])
            self.assertFalse(plan['production_ready']);self.assertEqual('v2.0.0',plan['tag'])
            self.assertEqual(set(COMPONENT_SERVICES),set(plan['images']))
            self.assertEqual(9,len(plan['compose_override']['services']))
            self.assertEqual(set(CONFIGURATION_PATHS),set(plan['configuration_sha256']))
            self.assertEqual(1,len(plan['migration_sha256']))
            self.assertFalse(validate_plan(plan,**inputs)['published'])

    def test_images_require_all_components_immutable_pull_evidence_and_exact_source_version(self):
        for kind in ('partial','tag','source','version','repository','digest','local_id'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                inputs=self.fixture(Path(directory));path=inputs['image_inspect'];images=json.loads(path.read_text())
                if kind=='partial':del images['executor']
                elif kind=='tag':images['api']['reference']='ghcr.io/example/ai-football-api:latest'
                elif kind=='source':images['api']['inspection']['Config']['Labels']['org.opencontainers.image.revision']='a'*40
                elif kind=='version':images['api']['inspection']['Config']['Labels']['org.opencontainers.image.version']='2.0.0-dev'
                elif kind=='repository':
                    images['api']['reference']=images['api']['reference'].replace('football-api','football-web')
                    images['api']['inspection']['RepoDigests']=[images['api']['reference']]
                elif kind=='digest':images['api']['inspection']['RepoDigests']=[]
                else:images['api']['inspection']['Id']='mutable'
                path.write_text(json.dumps(images))
                with self.assertRaises(ValueError):make_plan(**inputs)

    def test_dirty_prerelease_unsigned_wrong_device_and_missing_real_scopes_refused(self):
        for kind in ('dirty','manifest_dirty','rc','unsigned','device','missing','empty_notes'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                inputs=self.fixture(Path(directory));path=inputs['manifest'];record=json.loads(path.read_text())
                if kind=='dirty':inputs['worktree_status'].write_text(' M tracked-source')
                elif kind=='manifest_dirty':record['source_clean']=False
                elif kind=='rc':
                    record['version']='2.0.0-rc.1'
                    inputs['package_output'].write_text(inputs['package_output'].read_text().replace("versionName='2.0.0'","versionName='2.0.0-rc.1'"))
                elif kind=='unsigned':record['channel']='unsigned'
                elif kind=='device':
                    file=inputs['evidence']/'android_device.json';row=json.loads(file.read_text());row['apk_sha256']='e'*64;file.write_text(json.dumps(row))
                elif kind=='empty_notes':inputs['notes'].write_text('')
                else:(inputs['evidence']/'remote_restore.json').unlink()
                path.write_text(json.dumps(record))
                with self.assertRaises(ValueError):make_plan(**inputs)

    def test_validate_detects_plan_artifact_configuration_migration_and_note_changes(self):
        for kind in ('plan','artifact','configuration','migration','notes'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                inputs=self.fixture(Path(directory));plan=make_plan(**inputs)
                if kind=='plan':plan['images']['api']=plan['images']['web']
                elif kind=='artifact':inputs['apk'].write_bytes(b'tampered')
                elif kind=='configuration':(inputs['root']/CONFIGURATION_PATHS[0]).write_text('changed')
                elif kind=='migration':(inputs['root']/'services/api/migrations/001_fixture.sql').write_text('-- changed')
                else:inputs['notes'].write_text('changed')
                with self.assertRaises(ValueError):validate_plan(plan,**inputs)

    def test_cli_plan_and_validate_are_executable_without_network_or_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs=self.fixture(Path(directory))
            argv=['stable-plan','plan']
            for key,value in inputs.items():argv.extend(['--'+key.replace('_','-'),str(value)])
            output=io.StringIO()
            with patch('sys.argv',argv),patch.object(subprocess,'run',side_effect=AssertionError('no shell')),redirect_stdout(output):
                self.assertEqual(0,main())
            path=inputs['root']/'plan.json';path.write_text(output.getvalue())
            argv[1]='validate';argv.extend(['--plan',str(path)]);output=io.StringIO()
            with patch('sys.argv',argv),redirect_stdout(output):self.assertEqual(0,main())
            self.assertFalse(json.loads(output.getvalue())['published'])


if __name__=='__main__':
    unittest.main()
