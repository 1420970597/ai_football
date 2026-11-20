"""Synthetic APK metadata and fake ADB validate orchestration, never device UI."""
from argparse import Namespace
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

from infra.android.device_artifact import installed_version, validate_upgrade, verify_device_artifact
from tests.android.device_smoke import DeviceRun


class FakeAdb:
    def __init__(self, apk, code=20001, version='2.0.0-rc.1'):
        self.apk, self.code, self.version = apk, code, version
        self.calls = []
        self.fail = False
        self.upgrade = None

    def __call__(self, command, **kwargs):
        values = command[7:]
        self.calls.append(values)
        if self.fail:
            return subprocess.CompletedProcess(command, 1, b'', b'fixture secret must not be saved')
        if values[:3] == ['shell', 'dumpsys', 'package']:
            output = f' versionCode={self.code} minSdk=26\n versionName={self.version}\n'.encode()
        elif values[:3] == ['shell', 'pm', 'path']:
            output = b'package:/data/app/fixture/pkg/base.apk\n'
        elif values[:2] == ['exec-out', 'cat']:
            output = self.apk.read_bytes()
        elif values[:2] == ['install', '-r']:
            self.apk = Path(values[2])
            self.code, self.version = self.upgrade
            output = b'Success\n'
        else:
            output = b''
        return subprocess.CompletedProcess(command, 0, output, b'')


class NoUiRun(DeviceRun):
    def wait_text(self, *args, **kwargs):
        return ET.Element('node', {'bounds': '[0,0][20,20]'})

    def screenshot(self, name):
        self.artifacts.append({'explicit_fake_adb_screenshot': name})

    def input_service(self, url):
        pass

    def click(self, text, **kwargs):
        pass


class DeviceContractTests(unittest.TestCase):
    def artifact(self, root, *, code=20001, version='2.0.0-rc.1', signer='a'*64):
        folder = root / str(code)
        folder.mkdir()
        apk = folder / 'candidate.apk'
        apk.write_bytes(f'explicit synthetic APK fixture {code}'.encode())
        record = {'channel': 'signed-candidate', 'source_commit': 'b'*40, 'version': version,
                  'version_code': code, 'package_name': 'com.aifootball.platform',
                  'apk_sha256': hashlib.sha256(apk.read_bytes()).hexdigest(), 'certificate_sha256': signer}
        manifest = folder / 'manifest.json'
        manifest.write_text(json.dumps(record))
        output = folder / 'package.txt'
        output.write_text(f"package: name='com.aifootball.platform' versionCode='{code}' versionName='{version}'\n"
                          "uses-permission: name='android.permission.INTERNET'\n"
                          "uses-permission: name='android.permission.ACCESS_NETWORK_STATE'\n"
                          "Signer #1 certificate DN: CN=Explicit test fixture\n"
                          f"Signer #1 certificate SHA-256 digest: {signer}\n"
                          "Verified using v2 scheme (APK Signature Scheme v2): true\n")
        return manifest, apk, output

    def args(self, root, files, phase='first_launch', previous=None):
        previous = previous or (None, None, None)
        return Namespace(root=root, manifest=files[0], apk=files[1], package_output=files[2], phase=phase,
                         previous_manifest=previous[0], previous_apk=previous[1], previous_package_output=previous[2],
                         output=root/'evidence', adb='explicit-fake-adb', host='project-device', port='5037',
                         serial='explicit-selected-fixture', origin='https://fixture.example')

    def test_verified_manifest_selects_production_package_and_exact_installed_apk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); files = self.artifact(root); fake = FakeAdb(files[1])
            record = NoUiRun(self.args(root, files), command=fake).run()
            self.assertEqual('passed', record['status'])
            self.assertFalse(record['real_environment'])
            launches = [call for call in fake.calls if call[:3] == ['shell', 'am', 'start']]
            self.assertEqual('com.aifootball.platform/com.aifootball.platform.MainActivity', launches[0][-1])
            self.assertEqual(2, len(record['installed_version_chain']))
            self.assertEqual(hashlib.sha256(files[1].read_bytes()).hexdigest(), record['artifact']['apk_sha256'])
            self.assertIn(['exec-out', 'cat', '/data/app/fixture/pkg/base.apk'], fake.calls)

    def test_explicit_http_fixture_is_allowed_for_pairing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); files = self.artifact(root)
            args = self.args(root, files, 'pair'); args.origin = 'http://fixture.example:3003'
            record = NoUiRun(args, command=FakeAdb(files[1])).run()
            self.assertEqual('passed', record['status'])
            self.assertFalse(record['real_environment'])

    def test_certificate_rejection_phase_still_requires_tls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); files = self.artifact(root)
            args = self.args(root, files, 'untrusted_tls'); args.origin = 'http://fixture.example'
            fake = FakeAdb(files[1])
            with self.assertRaisesRegex(ValueError, 'HTTPS fixture'):
                NoUiRun(args, command=fake)
            self.assertEqual([], fake.calls)

    def test_failed_adb_is_recorded_without_raw_stderr_or_overwriting_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);files=self.artifact(root);fake=FakeAdb(files[1]);fake.fail=True
            args=self.args(root,files); record=NoUiRun(args,command=fake).run()
            self.assertEqual(1,record['exit_code']);self.assertEqual(1,record['operations'][0]['exit_code'])
            self.assertNotIn('fixture secret',json.dumps(record))
            with self.assertRaises(ValueError): NoUiRun(args,command=fake)

    def test_apk_tampering_metadata_mismatch_or_unknown_manifest_rejected_before_adb(self):
        for kind in ('tamper','package','channel','permission','symlink'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);files=list(self.artifact(root));fake=FakeAdb(files[1])
                if kind=='tamper': files[1].write_bytes(b'tampered')
                elif kind=='package': files[2].write_text(files[2].read_text().replace('com.aifootball.platform','wrong.app'))
                elif kind=='permission': files[2].write_text(files[2].read_text()+"uses-permission: name='android.permission.CAMERA'\n")
                elif kind=='symlink':
                    link=root/'linked.apk';link.symlink_to(files[1]);files[1]=link
                else:
                    record=json.loads(files[0].read_text());record['channel']='unsigned';files[0].write_text(json.dumps(record))
                with self.assertRaises(ValueError): NoUiRun(self.args(root,files),command=fake)
                self.assertEqual([],fake.calls)

    def test_installed_bytes_mismatch_fails_even_with_same_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);files=self.artifact(root)
            other=root/'other.apk';other.write_bytes(b'wrong bytes same metadata')
            record=NoUiRun(self.args(root,files),command=FakeAdb(other)).run()
            self.assertEqual('failed',record['status']);self.assertEqual([],record['installed_version_chain'])

    def test_upgrade_checks_both_versions_hashes_signer_and_increasing_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);previous=self.artifact(root,code=20001);current=self.artifact(root,code=20002,version='2.0.0-rc.2')
            fake=FakeAdb(previous[1]);fake.upgrade=(20002,'2.0.0-rc.2')
            record=NoUiRun(self.args(root,current,'upgrade',previous),command=fake).run()
            self.assertEqual('passed',record['status'])
            self.assertEqual([20001,20002,20002],[value['version_code'] for value in record['installed_version_chain']])
            self.assertEqual(['install','-r',str(current[1])],next(call for call in fake.calls if call[0]=='install'))
            self.assertIn('upgrade_session_continuity',record['remaining_acceptance'])
            old=verify_device_artifact(root,*previous);new=verify_device_artifact(root,*current)
            with self.assertRaises(ValueError):validate_upgrade(new,old)
            record=json.loads(current[0].read_text());record['certificate_sha256']='c'*64
            current[0].write_text(json.dumps(record));current[2].write_text(current[2].read_text().replace('a'*64,'c'*64))
            with self.assertRaises(ValueError):NoUiRun(self.args(root,current,'upgrade',previous),command=fake)

    def test_debug_asset_uses_manifest_identity_and_unsigned_assets_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);files=self.artifact(root);apk=files[1]
            record={'channel':'development','source_commit':'b'*40,'version':'2.0.0-dev','assets':{apk.name:{
                'sha256':hashlib.sha256(apk.read_bytes()).hexdigest(),'signing':'debug',
                'package_name':'com.aifootball.platform.dev','version_name':'2.0.0-dev+debug','version_code':20000}}}
            files[0].write_text(json.dumps(record))
            files[2].write_text(files[2].read_text().replace('com.aifootball.platform','com.aifootball.platform.dev')
                                .replace('20001','20000').replace('2.0.0-rc.1','2.0.0-dev+debug').replace('Explicit test fixture','Android Debug'))
            self.assertEqual('com.aifootball.platform.dev',verify_device_artifact(root,*files).package_name)
            record['assets'][apk.name]['signing']='unsigned';files[0].write_text(json.dumps(record))
            with self.assertRaises(ValueError):verify_device_artifact(root,*files)

    def test_installed_version_missing_ambiguous_or_wrong_device_version_refused(self):
        for value in ('', 'versionCode=1\nversionName=a\nversionCode=2'):
            with self.assertRaises(ValueError):installed_version(value)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);files=self.artifact(root)
            record=NoUiRun(self.args(root,files),command=FakeAdb(files[1],code=9)).run()
            self.assertEqual('failed',record['status'])

    def test_missing_ui_field_records_failure_and_malformed_manifest_is_rejected(self):
        class MissingUi(NoUiRun):
            def input_service(self, url):
                raise StopIteration
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);files=self.artifact(root)
            record=MissingUi(self.args(root,files),command=FakeAdb(files[1])).run()
            self.assertEqual('failed',record['status']);self.assertEqual('StopIteration',record['reason'])
            files[0].write_text('[]')
            with self.assertRaises(ValueError):verify_device_artifact(root,*files)


if __name__ == '__main__':
    unittest.main()
