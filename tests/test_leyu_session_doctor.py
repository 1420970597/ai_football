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
from typing import List
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
    """判定分支：IP 被拦 / 凭据无效 / 口令错 —— 方向不能反。"""

    def _run(self, real: dict, ctrl: dict) -> str:
        """用假响应跑 _report_credentials，捕获 stdout。"""
        import io
        import contextlib

        with mock.patch("urllib.request.urlopen") as uo:
            def _resp(payload: dict):
                m = mock.MagicMock()
                import json as _json
                m.read.return_value = _json.dumps(payload).encode()
                m.__enter__ = lambda s: s
                m.__exit__ = lambda *a: False
                return m

            calls = {"n": 0}

            def _side_effect(*_a, **_kw):
                calls["n"] += 1
                return _resp(real if calls["n"] == 1 else ctrl)

            uo.side_effect = _side_effect
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _report_credentials(
                    {"LEYU_APP_LOGIN_NAME": "u", "LEYU_APP_LOGIN_PASSWORD": "p"},
                    {})
            return buf.getvalue()

    def test_6031_means_ip_blocked(self) -> None:
        out = self._run({"status_code": 6031, "message": "地区ip限制"},
                        {"status_code": 6031, "message": "地区ip限制"})
        self.assertIn("IP 被拦", out)

    def test_6008_with_matching_control_means_bad_credentials(self) -> None:
        """服务端不区分「账号不存在」与「口令错」→ 只能判「凭据无效」。"""
        out = self._run({"status_code": 6008, "message": "用户名或密码错误"},
                        {"status_code": 6008, "message": "用户名或密码错误"})
        self.assertIn("凭据无效", out)
        self.assertNotIn("IP 被拦", out)

    def test_6008_without_matching_control_means_wrong_password(self) -> None:
        """账号存在但口令错 —— 与「账号不存在」可区分时要说清。"""
        out = self._run({"status_code": 6008, "message": "密码错误"},
                        {"status_code": 6002, "message": "账号不存在"})
        self.assertIn("口令错误", out)

    def test_unexpected_code_is_reported_not_guessed(self) -> None:
        out = self._run({"status_code": 9999, "message": "???"},
                        {"status_code": 6008, "message": "x"})
        self.assertIn("非预期响应", out)

    def test_no_credentials_says_so(self) -> None:
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _report_credentials({}, {})
        self.assertIn("未配置", buf.getvalue())

    def test_commented_credentials_are_used_when_active_absent(self) -> None:
        """生效配置为空时回退测注释行，并**标明来源**（否则用户会误以为已启用）。"""
        import io
        import contextlib
        import json as _json

        with mock.patch("urllib.request.urlopen") as uo:
            def _resp(payload: dict):
                m = mock.MagicMock()
                m.read.return_value = _json.dumps(payload).encode()
                m.__enter__ = lambda s: s
                m.__exit__ = lambda *a: False
                return m

            uo.return_value = _resp({"status_code": 6008, "message": "x"})
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _report_credentials({}, {"LEYU_APP_LOGIN_NAME": "a",
                                         "LEYU_APP_LOGIN_PASSWORD": "b"})
        out = buf.getvalue()
        self.assertIn("注释行", out)
        self.assertIn("无效", out)


if __name__ == "__main__":
    unittest.main()
