#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""App 账号登录续期的离线契约、冷却与拒绝保护测试。

这些 mock 用例不能证明线上账号登录成功或当前上游无需人机验证。

运行::

    python3 -m unittest tests.test_app_login -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from collector.leyu_app_login import (
    IP_RESTRICTED,
    LOGIN_ENV_NAME,
    LOGIN_ENV_PASSWORD,
    LOGIN_ENV_SIGNATURE,
    LOGIN_PATH,
    VERSION_TOO_LOW,
    AppLoginClient,
    AppLoginSessionProvider,
    LoginCredentials,
    LoginRejected,
    login_provider_from_env,
)
from collector.leyu_app_session import AppCredentials
from collector.session import Session, SessionError


def _session(rid: str = "rid1") -> Session:
    return Session(request_id=rid, host="https://api.x", origin="",
                   ttl_s=3600, note="")


class TestLoginCredentials(unittest.TestCase):
    def test_password_is_md5_hex(self) -> None:
        """口令必须以 32 位小写 MD5 发送（上游协议要求）。"""
        c = LoginCredentials(name="u", password="123456", uuid="uuid-1")
        self.assertEqual(c.password_md5, "e10adc3949ba59abbe56e057f20f883e")
        self.assertEqual(len(c.password_md5), 32)
        self.assertTrue(c.password_md5.islower())

    def test_masked_never_leaks_password(self) -> None:
        c = LoginCredentials(name="alice", password="s3cret", uuid="uuid-1")
        out = c.masked()
        self.assertNotIn("alice", out)
        self.assertNotIn("s3cret", out)
        self.assertNotIn("uuid-1", out)

    def test_missing_fields_raise(self) -> None:
        for kw in ({"name": ""}, {"password": ""}, {"uuid": ""}):
            base = {"name": "u", "password": "p", "uuid": "id"}
            base.update(kw)
            with self.subTest(kw=kw):
                with self.assertRaises(SessionError):
                    LoginCredentials(**base)


class TestAppLoginClient(unittest.TestCase):
    """登录客户端的请求契约与错误分类。"""

    def _client(self) -> AppLoginClient:
        return AppLoginClient(
            LoginCredentials(name="ff778580978", password="pw",
                             uuid="95ad64d5935fa539"),
            app_host="https://app.test", signature="sig")

    def test_login_success_returns_token(self) -> None:
        c = self._client()
        body = {"data": {"token": "NEWTOKEN", "userId": "1"},
                "message": "登录成功", "status_code": 6000}
        # 直接打桩 urlopen
        with mock.patch("urllib.request.urlopen") as uo:
            uo.return_value.__enter__.return_value.read.return_value = \
                json.dumps(body).encode()
            self.assertEqual(c.login(), "NEWTOKEN")

    def test_request_body_matches_capture(self) -> None:
        """请求体字段必须与抓包一致（否则上游会拒）。"""
        c = self._client()
        captured: dict = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            @staticmethod
            def read() -> bytes:
                return json.dumps(
                    {"data": {"token": "T"}, "status_code": 6000}).encode()

        def _fake_urlopen(req, timeout=None, context=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(
                (req.data or b"{}").decode("utf-8"))
            captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
            return _Resp()

        with mock.patch("urllib.request.urlopen", _fake_urlopen):
            c.login()

        self.assertTrue(captured["url"].endswith(LOGIN_PATH))
        b = captured["body"]
        self.assertEqual(b["uuid"], "95ad64d5935fa539")
        self.assertEqual(b["name"], "ff778580978")
        self.assertEqual(b["password"], c.credentials.password_md5)
        self.assertEqual(b["Flag"], 1)
        self.assertEqual(b["Version"], "2.0.1")
        self.assertEqual(b["Kaptchcate"], 99)
        # 登录**不需要** token（抓包实证），但可以带签名
        self.assertNotIn("x-api-token", captured["headers"])

    def test_ip_restriction_gives_actionable_error(self) -> None:
        """`6031 地区ip限制` 必须给出可操作指引，而不是含糊失败。"""
        c = self._client()

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            @staticmethod
            def read() -> bytes:
                return json.dumps({
                    "data": [], "message": "地区ip限制,不允许登录",
                    "status_code": IP_RESTRICTED}).encode()

        with mock.patch("urllib.request.urlopen", lambda *a, **k: _Resp()):
            with self.assertRaises(SessionError) as cm:
                c.login()
        msg = str(cm.exception)
        self.assertIn("地区", msg)
        self.assertIsInstance(cm.exception, LoginRejected)
        self.assertNotIn("无需检查口令", msg)

    def test_version_too_low_error(self) -> None:
        c = self._client()

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            @staticmethod
            def read() -> bytes:
                return json.dumps({
                    "data": [], "message": "版本过低", "status_code":
                    VERSION_TOO_LOW}).encode()

        with mock.patch("urllib.request.urlopen", lambda *a, **k: _Resp()):
            with self.assertRaises(SessionError) as cm:
                c.login()
        self.assertIn("版本", str(cm.exception))

    def test_missing_token_in_response_raises(self) -> None:
        c = self._client()

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            @staticmethod
            def read() -> bytes:
                return json.dumps({"data": {}, "status_code": 6000}).encode()

        with mock.patch("urllib.request.urlopen", lambda *a, **k: _Resp()):
            with self.assertRaises(SessionError):
                c.login()

    def test_non_json_response_raises(self) -> None:
        c = self._client()

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            @staticmethod
            def read() -> bytes:
                return b"<html>502</html>"

        with mock.patch("urllib.request.urlopen", lambda *a, **k: _Resp()):
            with self.assertRaises(SessionError):
                c.login()

    def test_rejected_response_never_echoes_upstream_secrets(self) -> None:
        c = self._client()
        with mock.patch("urllib.request.urlopen") as uo:
            uo.return_value.__enter__.return_value.read.return_value = json.dumps({
                "status_code": "6002", "message": "private-password private-token",
                "data": {"token": "private-token"},
            }).encode()
            with self.assertRaises(LoginRejected) as error:
                c.login()
        self.assertEqual(error.exception.status_code, "6002")
        self.assertNotIn("private-", str(error.exception) + c.last_error)
        self.assertIn("不能仅凭状态码", str(error.exception))

    def test_token_without_success_status_is_rejected(self) -> None:
        c = self._client()
        with mock.patch("urllib.request.urlopen") as uo:
            uo.return_value.__enter__.return_value.read.return_value = json.dumps({
                "data": {"token": "private-token"},
            }).encode()
            with self.assertRaises(SessionError):
                c.login()
        self.assertNotIn("private-token", c.last_error)


class TestAppLoginSessionProvider(unittest.TestCase):
    """provider 的续期策略：缓存复用 / 过期重登 / 非过期不重登 / 限流。"""

    def _setup(self, creds_ok: bool = True):
        client = AppLoginClient(
            LoginCredentials(name="u", password="p", uuid="uuid-1"),
            app_host="https://app.test", signature="sig")
        launcher = mock.MagicMock()
        launcher.credentials = AppCredentials("PLACEHOLDER", "uuid-1")
        return client, launcher

    def test_login_then_launch(self) -> None:
        client, launcher = self._setup()
        launcher.acquire.return_value = _session("rid1")
        with mock.patch.object(client, "login", return_value="T1"):
            p = AppLoginSessionProvider(client, launcher)
            self.assertEqual(p.acquire().request_id, "rid1")
            self.assertEqual(p.logins, 1)

    def test_cached_token_reused_without_login(self) -> None:
        client, launcher = self._setup()
        launcher.acquire.return_value = _session("rid2")
        with mock.patch.object(client, "login",
                               side_effect=AssertionError("不该登录")):
            p = AppLoginSessionProvider(client, launcher)
            p._token = "CACHED"
            p._token_at = 0.0
            self.assertEqual(p.acquire().request_id, "rid2")
            self.assertEqual(p.logins, 0)

    def test_expired_token_triggers_login(self) -> None:
        client, launcher = self._setup()
        launcher.acquire.side_effect = [
            SessionError("6001 token已过期"), _session("rid3")]
        with mock.patch.object(client, "login", return_value="T3") as m:
            p = AppLoginSessionProvider(client, launcher)
            p._token = "OLD"
            p._token_at = 0.0
            self.assertEqual(p.acquire().request_id, "rid3")
            self.assertEqual(m.call_count, 1)

    def test_non_expiry_error_does_not_login(self) -> None:
        """场馆维护等错误不应触发登录（那是白费且可能触发风控）。"""
        client, launcher = self._setup()
        launcher.acquire.side_effect = SessionError("6005 即将开放")
        with mock.patch.object(client, "login",
                               side_effect=AssertionError("不该登录")):
            p = AppLoginSessionProvider(client, launcher)
            p._token = "T"
            p._token_at = 0.0
            with self.assertRaises(SessionError):
                p.acquire()
            self.assertEqual(p.logins, 0)

    def test_invalidate_forces_relogin(self) -> None:
        client, launcher = self._setup()
        launcher.acquire.return_value = _session("rid5")
        with mock.patch.object(client, "login", return_value="T5"):
            p = AppLoginSessionProvider(client, launcher)
            p._token = "X"
            p._token_at = 0.0
            p.invalidate()
            self.assertFalse(p.cached_token)
            p.acquire()
            self.assertEqual(p.logins, 1)

    def test_relogin_throttled(self) -> None:
        """两次登录过近时跳过（上游限流 30 次/分钟）。"""
        client, launcher = self._setup()
        launcher.acquire.return_value = _session("rid6")
        with mock.patch.object(client, "login", return_value="T6"):
            p = AppLoginSessionProvider(client, launcher,
                                        min_relogin_interval_s=3600.0)
            self.assertEqual(p.acquire().request_id, "rid6")   # 首次登录
            # 立刻失效再试 → 应被限流挡住
            p.invalidate()
            with self.assertRaises(SessionError) as cm:
                p.acquire()
            self.assertIn("频繁", str(cm.exception))

    def test_failed_login_is_throttled(self) -> None:
        """凭据被拒时也必须冷却，避免实时重试线程打满登录接口。"""
        client, launcher = self._setup()
        with mock.patch.object(client, "login",
                               side_effect=SessionError("network timeout")) as m:
            p = AppLoginSessionProvider(client, launcher,
                                        min_relogin_interval_s=3600.0)
            with self.assertRaises(SessionError) as first:
                p.acquire()
            self.assertIn("timeout", str(first.exception))
            p.invalidate()
            with self.assertRaises(SessionError) as second:
                p.acquire()
            self.assertIn("频繁", str(second.exception))
            self.assertEqual(m.call_count, 1)
            self.assertEqual(p.login_attempts, 1)

    def test_business_rejection_stops_login_after_cooldown_and_invalidate(self) -> None:
        for code in (6002, 6008, 6003, 6022, 6030, 6031, 6606):
            with self.subTest(code=code):
                client, launcher = self._setup()
                p = AppLoginSessionProvider(client, launcher)
                with mock.patch("urllib.request.urlopen") as uo:
                    uo.return_value.__enter__.return_value.read.return_value = json.dumps({
                        "status_code": code, "message": "private-server-message",
                    }).encode()
                    with mock.patch("time.monotonic", return_value=100):
                        with self.assertRaises(LoginRejected):
                            p.acquire()
                    p.invalidate()
                    with mock.patch("time.monotonic", return_value=10000):
                        with self.assertRaises(SessionError) as error:
                            p.acquire()
                    self.assertIn("停止自动账号登录", str(error.exception))
                    self.assertEqual(uo.call_count, 1)
                self.assertTrue(p.describe()["login_blocked"])
                self.assertEqual(p.login_attempts, 1)
                launcher.acquire.assert_not_called()

    def test_network_failure_retries_after_cooldown_and_clears_error(self) -> None:
        client, launcher = self._setup()
        launcher.acquire.return_value = _session("rid-recovered")
        p = AppLoginSessionProvider(client, launcher)
        with mock.patch.object(client, "login", side_effect=[SessionError("timeout"), "T"]) as login:
            with mock.patch("time.monotonic", return_value=100):
                with self.assertRaises(SessionError):
                    p.acquire()
            self.assertEqual(p.last_error, "timeout")
            with mock.patch("time.monotonic", return_value=131):
                self.assertEqual(p.acquire().request_id, "rid-recovered")
            self.assertEqual(login.call_count, 2)
        self.assertFalse(p.describe()["login_blocked"])
        self.assertEqual(p.last_error, "")

    def test_launch_and_login_are_serialized(self) -> None:
        """并发刷新共享启动器时只能产生一次登录请求。"""
        import threading

        client, launcher = self._setup()
        launcher.acquire.return_value = _session("rid-concurrent")
        started = threading.Event()
        release = threading.Event()
        second_waiting = threading.Event()
        inner_lock = threading.RLock()

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread().name == "second-acquire":
                    second_waiting.set()
                return inner_lock.__enter__()

            def __exit__(self, *args):
                return inner_lock.__exit__(*args)

        def login() -> str:
            started.set()
            if not release.wait(2):
                raise AssertionError("login was not released")
            return "T-concurrent"

        with mock.patch.object(client, "login", side_effect=login) as m:
            p = AppLoginSessionProvider(client, launcher,
                                        min_relogin_interval_s=0.0)
            lock_patch = mock.patch.object(p, "_lock", ObservedLock())
            lock_patch.start()
            self.addCleanup(lock_patch.stop)
            errors = []

            def run() -> None:
                try:
                    p.acquire()
                except Exception as exc:  # pragma: no cover - assertion below
                    errors.append(exc)

            threads = [threading.Thread(target=run, daemon=True) for _ in range(2)]
            threads[1].name = "second-acquire"
            threads[0].start()
            self.assertTrue(started.wait(1))
            threads[1].start()
            self.assertTrue(second_waiting.wait(1))
            release.set()
            for t in threads:
                t.join(timeout=2)
                self.assertFalse(t.is_alive())
            self.assertFalse(errors)
            self.assertEqual(m.call_count, 1)
            self.assertEqual(p.logins, 1)

    def test_token_cached_to_disk_and_reloaded(self) -> None:
        client, launcher = self._setup()
        launcher.acquire.return_value = _session("rid7")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tok.json")
            with mock.patch.object(client, "login", return_value="PERSIST"):
                p = AppLoginSessionProvider(client, launcher,
                                            token_cache_path=path)
                p.acquire()
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["token"], "PERSIST")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            # 新实例应直接读到缓存（不再登录）
            p2 = AppLoginSessionProvider(client, launcher,
                                         token_cache_path=path)
            self.assertEqual(p2.cached_token, "PERSIST")

    def test_corrupt_cache_is_tolerated(self) -> None:
        client, launcher = self._setup()
        launcher.acquire.return_value = _session("rid8")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tok.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            with mock.patch.object(client, "login", return_value="T8"):
                p = AppLoginSessionProvider(client, launcher,
                                            token_cache_path=path)
                self.assertEqual(p.cached_token, "")
                p.acquire()
                self.assertEqual(p.logins, 1)

    def test_launcher_without_writable_credentials(self) -> None:
        """启动器缺可写凭据时给出明确错误（而非 TypeError）。"""
        client = AppLoginClient(
            LoginCredentials(name="u", password="p", uuid="uuid-1"),
            app_host="https://app.test")
        with mock.patch.object(client, "login", return_value="T"):
            p = AppLoginSessionProvider(client, object())
            with self.assertRaises(SessionError) as cm:
                p.acquire()
            self.assertIn("credentials", str(cm.exception))


class TestLoginProviderFromEnv(unittest.TestCase):
    def test_login_and_launch_use_separate_prefix_signatures(self) -> None:
        env = {
            LOGIN_ENV_NAME: "account", LOGIN_ENV_PASSWORD: "password",
            "LEYU_APP_UUID": "device", "LEYU_APP_SIGNATURE": "game-signature",
            LOGIN_ENV_SIGNATURE: "site-signature",
        }
        p = login_provider_from_env(env)
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.client._headers()["x-api-xxx"], "site-signature")
        self.assertEqual(p.launcher.signature, "game-signature")
        env.pop(LOGIN_ENV_SIGNATURE)
        p2 = login_provider_from_env(env)
        assert p2 is not None
        self.assertNotIn("x-api-xxx", p2.client._headers())
        self.assertEqual(p2.launcher.signature, "game-signature")

    def test_none_without_credentials(self) -> None:
        self.assertIsNone(login_provider_from_env({}))
        self.assertIsNone(login_provider_from_env(
            {LOGIN_ENV_NAME: "u"}))          # 缺口令
        self.assertIsNone(login_provider_from_env(
            {LOGIN_ENV_NAME: "u", LOGIN_ENV_PASSWORD: "p"}))   # 缺 uuid

    def test_built_with_credentials(self) -> None:
        p = login_provider_from_env({
            LOGIN_ENV_NAME: "u", LOGIN_ENV_PASSWORD: "p",
            "LEYU_APP_UUID": "uuid-1", "LEYU_APP_SIGNATURE": "sig"})
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.name, "app-login")
        d = p.describe()
        self.assertEqual(d["provider"], "app-login")
        self.assertFalse(d["has_cached_token"])
