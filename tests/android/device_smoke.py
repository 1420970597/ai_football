"""Device-fixture acceptance bound to a verified APK; never production evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from infra.android.device_artifact import installed_version, validate_upgrade, verify_device_artifact

PHASES = ('first_launch', 'untrusted_tls', 'pair', 'flows', 'clear_login', 'upgrade')


class DeviceRun:
    def __init__(self, args, *, command=subprocess.run):
        self.args = args
        origin = urlsplit(args.origin)
        if origin.scheme not in {'http', 'https'} or not origin.hostname or origin.username or origin.password or origin.path not in {'', '/'} or origin.query or origin.fragment:
            raise ValueError('device fixture requires an explicit HTTP/HTTPS origin without credentials')
        if args.phase == 'untrusted_tls' and origin.scheme != 'https':
            raise ValueError('untrusted_tls phase requires an HTTPS fixture')
        self.artifact = verify_device_artifact(args.root, args.manifest, args.apk, args.package_output)
        self.previous = None
        previous_paths = (args.previous_manifest, args.previous_apk, args.previous_package_output)
        if args.phase == 'upgrade':
            if not all(previous_paths):
                raise ValueError('upgrade requires all three verified previous artifact inputs')
            self.previous = verify_device_artifact(args.root, *previous_paths)
            validate_upgrade(self.previous, self.artifact)
        elif any(previous_paths):
            raise ValueError('previous artifact inputs are only valid for the upgrade phase')
        self.output = args.output if args.output.is_absolute() else args.root / args.output
        root = args.root.resolve()
        if root not in self.output.resolve().parents:
            raise ValueError('device evidence output must remain inside the project')
        for path in [self.output, *self.output.parents]:
            if path == root:
                break
            if path.is_symlink():
                raise ValueError('device evidence output cannot use symbolic links')
        self.output.mkdir(parents=True, exist_ok=True)
        self.record_path = self.output / f'{args.phase}.json'
        if self.record_path.exists():
            raise ValueError('preserve previous device phase evidence before rerunning')
        self.command = command
        self.operations = []
        self.artifacts = []
        self.versions = []

    def adb(self, *values, binary=False):
        base = [self.args.adb, '-H', self.args.host, '-P', self.args.port, '-s', self.args.serial]
        started = time.monotonic()
        result = self.command(base + list(values), capture_output=True, timeout=50, check=False)
        self.operations.append({'operation': list(values[:3]), 'exit_code': result.returncode,
                                'seconds': round(time.monotonic() - started, 3)})
        if result.returncode:
            raise RuntimeError('adb_command_failed')
        return result.stdout if binary else result.stdout.decode('utf-8', errors='replace')

    def tree(self):
        self.adb('shell', 'uiautomator', 'dump', '/sdcard/ballnews-window.xml')
        return ET.fromstring(self.adb('shell', 'cat', '/sdcard/ballnews-window.xml'))

    def wait_text(self, text, *, exact=False, seconds=45):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for node in self.tree().iter('node'):
                value = node.get('text', '')
                if value == text if exact else text in value:
                    return node
            time.sleep(1)
        raise AssertionError('expected_fixture_ui_text_missing')

    def tap(self, node):
        left, top, right, bottom = map(int, re.findall(r'\d+', node.attrib['bounds']))
        self.adb('shell', 'input', 'tap', str((left + right) // 2), str((top + bottom) // 2))

    def click(self, text, *, exact=True):
        self.tap(self.wait_text(text, exact=exact))

    def input_service(self, url):
        node = next(n for n in self.tree().iter('node') if n.get('resource-id', '').endswith(':id/service_input'))
        self.tap(node)
        self.adb('shell', 'input', 'keyevent', '123')
        self.adb('shell', 'input', 'keyevent', *('67' for _ in range(len(node.get('text', '')) + 1)))
        self.adb('shell', 'input', 'text', url)
        self.adb('shell', 'input', 'keyevent', '4')

    def screenshot(self, name):
        for suffix, value in [('png', self.adb('exec-out', 'screencap', '-p', binary=True)),
                              ('xml', ET.tostring(self.tree(), encoding='utf-8'))]:
            path = self.output / f'{name}.{suffix}'
            with path.open('xb') as stream:
                stream.write(value)
            self.artifacts.append({'path': str(path.relative_to(self.args.root.resolve())),
                                   'sha256': hashlib.sha256(value).hexdigest()})

    def assert_installed(self, artifact):
        version = installed_version(self.adb('shell', 'dumpsys', 'package', artifact.package_name))
        if version != {'version_code': artifact.version_code, 'version_name': artifact.version_name}:
            raise ValueError('installed package does not match the verified artifact version')
        paths = self.adb('shell', 'pm', 'path', artifact.package_name).splitlines()
        if len(paths) != 1 or not re.fullmatch(r'package:/data/app/[A-Za-z0-9_./=+~-]+/base\.apk', paths[0]):
            raise ValueError('device probe requires exactly one installed base APK')
        digest = hashlib.sha256(self.adb('exec-out', 'cat', paths[0][8:], binary=True)).hexdigest()
        if digest != artifact.apk_sha256:
            raise ValueError('installed APK bytes do not match the verified artifact')
        self.versions.append({'package_name': artifact.package_name, **version,
                              'apk_sha256': artifact.apk_sha256})

    def phase(self):
        phase = self.args.phase
        self.assert_installed(self.previous if phase == 'upgrade' else self.artifact)
        if phase == 'first_launch':
            self.adb('shell', 'am', 'start', '-W', '-n',
                     self.artifact.package_name + '/com.aifootball.platform.MainActivity')
            self.wait_text('连接球讯服务'); self.screenshot('01-first-launch')
            self.input_service('ftp://unsafe.invalid'); self.click('验证并连接')
            self.wait_text('请使用有效'); self.screenshot('02-unsafe-scheme-blocked')
        elif phase == 'untrusted_tls':
            self.input_service(self.args.origin); self.click('验证并连接')
            self.wait_text('安全证书验证失败'); self.screenshot('03-untrusted-certificate')
        elif phase == 'pair':
            self.input_service(self.args.origin); self.click('验证并连接')
            self.wait_text('确认连接服务'); self.screenshot('04-pair-confirmation')
            self.click('连接'); self.wait_text('比分首页（隔离样本）'); self.screenshot('05-paired-webview')
        elif phase == 'flows':
            self.click('专家阅读'); self.wait_text('登录（隔离样本）'); self.click('登录样本')
            self.wait_text('专家推荐（隔离样本）'); self.screenshot('06-session-and-expert-reading')
            self.click('查看样本详情'); self.wait_text('推荐详情（隔离样本）')
            self.adb('shell', 'input', 'keyevent', '4'); self.wait_text('专家推荐（隔离样本）')
            self.click('模拟会话过期'); self.wait_text('登录（隔离样本）'); self.screenshot('07-expired-session')
            self.click('设置'); self.click('刷新页面'); self.wait_text('比分首页（隔离样本）')
            self.click('外部网站'); self.wait_text('打开外部链接', exact=True); self.screenshot('08-external-confirmation')
            self.click('取消'); self.click('危险地址'); self.wait_text('比分首页（隔离样本）')
            self.click('模拟服务故障'); self.wait_text('服务返回错误（503）'); self.screenshot('09-http-failure')
            self.click('重试'); self.wait_text('比分首页（隔离样本）')
        elif phase == 'clear_login':
            self.click('专家阅读'); self.click('登录样本'); self.wait_text('专家推荐（隔离样本）')
            self.click('设置'); self.click('清除本机登录'); self.wait_text('将清除此应用内')
            self.click('清除本机登录'); self.wait_text('比分首页（隔离样本）')
            self.click('专家阅读'); self.wait_text('登录（隔离样本）'); self.screenshot('10-cleared-login')
        else:
            self.adb('install', '-r', str(self.artifact.apk_path))
            self.assert_installed(self.artifact)
            self.adb('shell', 'am', 'start', '-W', '-n',
                     self.artifact.package_name + '/com.aifootball.platform.MainActivity')
            self.wait_text('比分首页（隔离样本）'); self.screenshot('11-upgrade-pairing-retained')
        self.assert_installed(self.artifact)

    def run(self):
        started = time.monotonic()
        status, exit_code, reason = 'failed', 1, None
        try:
            self.phase()
            status, exit_code = 'passed', 0
        except (OSError, RuntimeError, ValueError, AssertionError, StopIteration, ET.ParseError, subprocess.TimeoutExpired) as exc:
            reason = type(exc).__name__
        record = {'scope': 'android_device_phase', 'phase': self.args.phase,
                  'status': status, 'exit_code': exit_code, 'reason': reason,
                  'real_environment': False, 'environment': 'authorized_device_with_isolated_pages',
                  'checked_at': datetime.now(timezone.utc).isoformat(),
                  'artifact': self.artifact.evidence(),
                  'previous_artifact': self.previous.evidence() if self.previous else None,
                  'installed_version_chain': self.versions, 'operations': self.operations,
                  'artifacts': self.artifacts, 'seconds': round(time.monotonic() - started, 3),
                  'remaining_acceptance': ['production_identity', 'rotation_large_font', 'server_change',
                                           'network_loss_recovery', 'uninstall_reinstall', 'downgrade_rejection',
                                           'upgrade_session_continuity']}
        with self.record_path.open('x') as stream:
            stream.write(json.dumps(record, indent=2, sort_keys=True) + '\n')
        return record


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument('phase', choices=PHASES)
    value.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    value.add_argument('--manifest', type=Path, required=True)
    value.add_argument('--apk', type=Path, required=True)
    value.add_argument('--package-output', type=Path, required=True)
    for key in ('manifest', 'apk', 'package-output'):
        value.add_argument('--previous-' + key, type=Path)
    value.add_argument('--adb', default='/opt/android-sdk/platform-tools/adb')
    value.add_argument('--host', required=True)
    value.add_argument('--port', default='5037')
    value.add_argument('--serial', required=True)
    value.add_argument('--output', type=Path, required=True)
    value.add_argument('--origin', required=True)
    return value


def main():
    try:
        args = parser().parse_args()
        args.root = args.root.resolve()
        record = DeviceRun(args).run()
        print(json.dumps({'phase': args.phase, 'status': record['status'], 'exit_code': record['exit_code'],
                          'apk_sha256': record['artifact']['apk_sha256'], 'real_environment': False}))
        return record['exit_code']
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({'status': 'rejected', 'reason': type(exc).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
