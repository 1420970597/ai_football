#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/leyu_session_doctor.py` 的单元测试。

离线验证 Compose 配置读取、脱敏、默认不登录和显式登录只提交一次。
mock 不能证明当前上游账号登录成功。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from typing import List
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))

# tools/ 不是包（无 __init__.py），导入路径靠在上面手工插入。
# 所有 HTTP 与 Compose 配置查询均在用例中隔离。
import leyu_session_doctor as doctor  # noqa: E402
from leyu_session_doctor import (  # noqa: E402
    _disabled_credentials,
    _fp,
    _report_credentials,
)


def _write_env(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".env")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class DoctorDisabledCredentialsTest(unittest.TestCase):
    """被注释掉的凭据必须被识别出来（本项目真实踩过的坑）。"""

    def setUp(self) -> None:
        self._paths: List[str] = []

    def tearDown(self) -> None:
        for p in self._paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _env(self, text: str) -> str:
        p = _write_env(text)
        self._paths.append(p)
        return p

    def test_finds_commented_leyu_credentials(self) -> None:
        """核心场景：凭据存在但整行被 # 注释 —— 普通解析会看不见。"""
        p = self._env(
            "LEYU_APP_TOKEN=abc\n"
            "# LEYU_APP_LOGIN_NAME=someone\n"
            "#LEYU_APP_LOGIN_PASSWORD=hunter2\n"
        )
        found = _disabled_credentials(p)
        self.assertEqual(found.get("LEYU_APP_LOGIN_NAME"), "someone")
        self.assertEqual(found.get("LEYU_APP_LOGIN_PASSWORD"), "hunter2")

    def test_ignores_active_lines(self) -> None:
        """生效行不算「被注释」——否则会误报。"""
        p = self._env("LEYU_APP_LOGIN_NAME=active\n")
        self.assertEqual(_disabled_credentials(p), {})

    def test_ignores_empty_commented_values(self) -> None:
        """`# KEY=` 没有值 —— 报了也没用，只会制造噪音。"""
        p = self._env("# LEYU_APP_LOGIN_NAME=\n# LEYU_APP_LOGIN_PASSWORD=   \n")
        self.assertEqual(_disabled_credentials(p), {})

    def test_ignores_unrelated_commented_keys(self) -> None:
        """只关心凭据类键，不要把整份 .env 的注释都倒出来。"""
        p = self._env("# SOME_OTHER_SETTING=1\n# LEYU_VENUE=YBTY\n")
        found = _disabled_credentials(p)
        self.assertNotIn("SOME_OTHER_SETTING", found)

    def test_comment_with_prose_is_not_a_key(self) -> None:
        """纯注释文字（无 =）不得被当成键值对。"""
        p = self._env("# 这是在允许的网络上使用的凭据，本机 IP 被限制\n")
        self.assertEqual(_disabled_credentials(p), {})

    def test_missing_file_is_not_fatal(self) -> None:
        """文件不存在 → 空字典，不能抛（诊断工具本身不能崩）。"""
        self.assertEqual(_disabled_credentials("/nonexistent/.env"), {})


class DoctorFingerprintTest(unittest.TestCase):
    """脱敏输出：诊断要能贴给别人看，不能泄漏明文。"""

    def test_masks_middle_of_long_value(self) -> None:
        value = "example-private-token" * 4
        out = _fp(value)
        self.assertIn("len=", out)
        self.assertIn("sha256=", out)
        self.assertNotIn("example", out)
        self.assertEqual(out, _fp(value))

    def test_empty_value_marked_empty(self) -> None:
        self.assertIn("empty", _fp(""))

    def test_short_value_not_leaked_verbatim(self) -> None:
        """短值也要走指纹，不能原样打出来。"""
        out = _fp("abc")
        self.assertNotIn("abc", out.replace("head=", "head="))


class DoctorCredentialVerdictTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name

    def _env(self):
        return {
            "LEYU_APP_LOGIN_NAME": "example-user",
            "LEYU_APP_LOGIN_PASSWORD": "example-password",
            "LEYU_APP_UUID": "example-device",
            "LEYU_APP_HOST": "https://app.test",
            "LEYU_APP_SIGNATURE": "a" * 64,
            "LEYU_APP_LOGIN_STATE": os.path.join(self.directory, "guard.json"),
            "LEYU_SESSION_CACHE": os.path.join(self.directory, "_session.json"),
        }

    def _run(self, login_reply, allow_login=False):
        import contextlib
        import io
        import json

        sent = []

        def response(request, **kwargs):
            sent.append(request)
            if request.full_url.endswith("/user/login"):
                payload = login_reply
            elif request.full_url.endswith("/venue/launch"):
                payload = {"status_code": 6000, "data": {"url": "https://api.test?token=business-session"}}
            else:
                raise AssertionError("Unexpected request")
            obj = mock.MagicMock()
            obj.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            return obj

        buf = io.StringIO()
        with mock.patch.object(doctor, "_environment", return_value=self._env()), \
                mock.patch.object(doctor, "_disabled_credentials", return_value={}), \
                mock.patch("urllib.request.urlopen", side_effect=response), \
                mock.patch("collector.leyu_client.LEYUClient") as client, \
                contextlib.redirect_stdout(buf):
            client.return_value.all_matches.return_value = []
            status = doctor.main(["--login"] if allow_login else [])
        return status, buf.getvalue(), sent, client

    def test_default_diagnostic_never_submits_credentials(self):
        status, out, sent, client = self._run({})
        self.assertEqual(sent, [])
        self.assertEqual(status, 1)
        self.assertIn("本次未提交", out)
        self.assertNotIn("example-password", out)
        client.assert_not_called()

    def test_explicit_login_uses_production_protocol_once_and_reuses_session(self):
        import hashlib
        import json

        status, out, sent, client = self._run(
            {"status_code": 6000, "data": {"token": "private-app-token"}}, allow_login=True)
        self.assertEqual(status, 0)
        self.assertEqual(sum(r.full_url.endswith("/user/login") for r in sent), 1)
        self.assertEqual(len(sent), 2)
        body = json.loads(sent[0].data)
        self.assertEqual(body["name"], "example-user")
        self.assertEqual(body["password"], hashlib.md5(b"example-password").hexdigest())
        self.assertEqual(body["Kaptchcate"], 99)
        self.assertEqual(client.call_args.kwargs["request_id"], "business-session")
        self.assertNotIn("private-app-token", out)
        self.assertNotIn("example-password", out)
        self.assertNotIn("example-user", out)

    def test_rejection_is_reported_once_without_echo_or_password_verdict(self):
        status, out, sent, client = self._run(
            {"status_code": 6002, "message": "private-server-response"}, allow_login=True)
        self.assertEqual(status, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("6002", out)
        self.assertNotIn("private-server-response", out)
        self.assertNotIn("口令错误", out)
        client.assert_not_called()

    def test_repeated_doctor_invocations_share_login_rejection(self):
        self._run({"status_code": 6002}, allow_login=True)
        status, out, sent, client = self._run({"status_code": 6000}, allow_login=True)
        self.assertEqual(status, 1)
        self.assertEqual(sent, [])
        self.assertIn("6002", out)
        client.assert_not_called()

    def test_doctor_retains_production_token_cache_without_duplicate_login(self):
        self._run({"status_code": 6000, "data": {"token": "cached-app-token"}}, allow_login=True)
        self.assertTrue(os.path.exists(os.path.join(self.directory, "_app_token.json")))
        status, _, sent, _ = self._run({}, allow_login=True)
        self.assertEqual(status, 0)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].full_url.endswith("/venue/launch"))

    def test_commented_credentials_are_reported_but_never_enabled(self):
        import io
        import contextlib

        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen") as request, contextlib.redirect_stdout(buf):
            _report_credentials({}, {"LEYU_APP_LOGIN_NAME": "example-user",
                                     "LEYU_APP_LOGIN_PASSWORD": "example-password"}, allow_login=True)
        request.assert_not_called()
        self.assertIn("注释行", buf.getvalue())
        self.assertNotIn("example-password", buf.getvalue())

    def test_compose_environment_values_are_preserved(self):
        import json

        data = {"services": {"analytics-api": {"environment": {
            "LEYU_APP_LOGIN_PASSWORD": "quoted$example-password",
        }}}}
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("os.path.exists", return_value=True), \
                mock.patch("subprocess.check_output", return_value=json.dumps(data)) as compose:
            env = doctor._environment("/repo/.env")
        self.assertEqual(env["LEYU_APP_LOGIN_PASSWORD"], "quoted$example-password")
        self.assertIn("--format", compose.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
