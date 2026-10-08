#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/leyu_session_doctor.py` 的单元测试。

只测**纯逻辑**（.env 解析、凭据判定分支、指纹脱敏），不碰网络：
诊断工具的价值在于「把 IP 被拦 与 凭据失效 区分开」，
这个区分逻辑必须被测死，否则会把排障方向指反。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from typing import Any, List, Optional
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))

# tools/ 不是包（无 __init__.py），导入路径靠在上面手工插入。
# 单测的目的就是把「IP 被拦 / 凭据无效」的判定钉死。
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
        out = _fp("e0a669ad474c33a0db386812cfc5a734faa9da1ad60a187142b7e35b2")
        self.assertIn("len=", out)
        self.assertIn("head=e0a6", out)
        self.assertNotIn("fa9da1ad60a187142b7e35b2", out)  # 尾部不外泄

    def test_empty_value_marked_empty(self) -> None:
        self.assertIn("empty", _fp(""))

    def test_short_value_not_leaked_verbatim(self) -> None:
        """短值也要走指纹，不能原样打出来。"""
        out = _fp("abc")
        self.assertNotIn("abc", out.replace("head=", "head="))


class DoctorCredentialVerdictTest(unittest.TestCase):
    """凭据诊断必须走生产登录协议，并避免泄漏 token 或响应内容。"""

    def _run(self, payload: dict, env: Optional[dict] = None,
             commented: Optional[dict] = None) -> tuple[str, Any]:
        import io
        import contextlib
        import json as _json

        with mock.patch("urllib.request.urlopen") as uo:
            def _resp():
                m = mock.MagicMock()
                m.read.return_value = _json.dumps(payload).encode()
                m.__enter__ = lambda s: s
                m.__exit__ = lambda *a: False
                return m

            uo.return_value = _resp()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _report_credentials(env or {
                    "LEYU_APP_LOGIN_NAME": "real-account",
                    "LEYU_APP_LOGIN_PASSWORD": "real-password",
                    "LEYU_APP_UUID": "configured-uuid",
                    "LEYU_APP_HOST": "https://app.test",
                }, commented or {})
            return buf.getvalue(), uo

    def test_6031_means_ip_blocked(self) -> None:
        out, _ = self._run({"status_code": 6031, "message": "地区ip限制"})
        self.assertIn("地区 IP 限制", out)

    def test_credential_rejection_is_not_overinterpreted(self) -> None:
        out, _ = self._run({"status_code": 6002, "message": "用户名或密码错误"})
        self.assertIn("不足以判断", out)
        self.assertNotIn("IP 被拦", out)

    def test_production_request_contains_configured_password_hash(self) -> None:
        import hashlib
        import json as _json

        secret = "real-password"
        out, call = self._run({"status_code": 6000,
                               "data": {"token": "must-not-be-printed"},
                               "message": "登录成功"})
        request = call.call_args.args[0]
        body = _json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["name"], "real-account")
        self.assertEqual(body["password"], hashlib.md5(secret.encode()).hexdigest())
        self.assertNotEqual(body["password"], secret)
        self.assertEqual(body["uuid"], "configured-uuid")
        self.assertIn("生产登录成功", out)
        self.assertNotIn("must-not-be-printed", out)
        self.assertNotIn(secret, out)
        self.assertEqual(call.call_count, 1)

    def test_no_credentials_says_so(self) -> None:
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _report_credentials({}, {})
        self.assertIn("未配置", buf.getvalue())

    def test_commented_credentials_are_used_when_active_absent(self) -> None:
        """注释值可以被核验，但输出明确标注它没有注入运行环境。"""
        out, call = self._run(
            {"status_code": 6002, "message": "拒绝"},
            {"LEYU_APP_UUID": "configured-uuid"},
            {"LEYU_APP_LOGIN_NAME": "commented-account",
             "LEYU_APP_LOGIN_PASSWORD": "commented-password"})
        body = __import__("json").loads(
            call.call_args.args[0].data.decode("utf-8"))
        self.assertIn("注释行", out)
        self.assertEqual(body["name"], "commented-account")
        self.assertIn("6002", out)


if __name__ == "__main__":
    unittest.main()
