#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话生命周期与自动续期的单元测试。

覆盖：
  - Session 值对象：脱敏、过期判定、字段校验
  - 三种 provider：环境变量 / 文件 / 登录命令（含错误路径）
  - provider 链的回退顺序
  - **自动续期**：业务返回 0401013 时重新 acquire 并重试（核心回归）
  - 非鉴权错误**不得**触发重新登录（防止误把业务问题当鉴权问题）

运行：``python3 -m unittest tests.test_session -v``
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Mapping, Optional

from collector.leyu_client import AuthError, DecodeError, LEYUMatch
from collector.session import (
    DEFAULT_SESSION_TTL_S,
    ChainSessionProvider,
    CommandSessionProvider,
    EnvSessionProvider,
    FileSessionProvider,
    NullSessionProvider,
    Session,
    SessionError,
    SessionProvider,
    SESSION_ENV_COMMAND,
    SESSION_ENV_CUID,
    SESSION_ENV_FILE,
    SESSION_ENV_REQUEST_ID,
    make_session_provider,
)
from collector.sources import LEYUSource


# --------------------------------------------------------------------------- #
# Session 值对象
# --------------------------------------------------------------------------- #

class TestSession(unittest.TestCase):
    def test_empty_request_id_rejected(self) -> None:
        for bad in ("", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(SessionError):
                    Session(request_id=bad)

    def test_masked_hides_token(self) -> None:
        # 用**合成**令牌，绝不能用抓包/线上的真实会话值（AGENTS.md §3.4）
        token = "deadbeefcafebabe0123456789abcdef"
        s = Session(request_id=token, cuid="100000000000000001")
        m = s.masked()
        self.assertIn(token[:8], m)
        # 完整令牌绝不能出现在可打印文本里
        self.assertNotIn(token, m)

    def test_as_dict_masks_secrets_by_default(self) -> None:
        s = Session(request_id="secret-token-value-123456", cookie="k=v")
        d = s.as_dict()
        self.assertEqual(d["request_id"], "***")
        self.assertEqual(d["cookie"], "***")
        self.assertNotIn("secret-token-value-123456", json.dumps(d))
        # 显式要求才输出明文
        self.assertEqual(s.as_dict(include_secrets=True)["request_id"],
                         "secret-token-value-123456")

    def test_expiry(self) -> None:
        fresh = Session(request_id="r", ttl_s=3600)
        self.assertFalse(fresh.expired)
        old = Session(request_id="r", ttl_s=1800,
                      obtained_at=datetime.now(timezone.utc) - timedelta(hours=2))
        self.assertTrue(old.expired)
        self.assertGreater(old.age_s, 7000)

    def test_ttl_zero_means_no_expiry(self) -> None:
        s = Session(request_id="r", ttl_s=0,
                    obtained_at=datetime.now(timezone.utc) - timedelta(days=30))
        self.assertFalse(s.expired)

    def test_default_ttl_matches_capture_observation(self) -> None:
        # 实测抓包会话约 1.5~2 小时失效
        self.assertEqual(DEFAULT_SESSION_TTL_S, 1800.0)


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #

class TestEnvProvider(unittest.TestCase):
    def test_reads_all_fields(self) -> None:
        p = EnvSessionProvider({SESSION_ENV_REQUEST_ID: "rid-1",
                                SESSION_ENV_CUID: "cuid-1",
                                "LEYU_COOKIE": "X-API-TOKEN=abc"})
        s = p.acquire()
        self.assertEqual(s.request_id, "rid-1")
        self.assertEqual(s.cuid, "cuid-1")
        self.assertEqual(s.cookie, "X-API-TOKEN=abc")

    def test_missing_raises_actionable(self) -> None:
        with self.assertRaises(SessionError) as ctx:
            EnvSessionProvider({}).acquire()
        self.assertIn(SESSION_ENV_REQUEST_ID, str(ctx.exception))

    def test_whitespace_only_treated_as_missing(self) -> None:
        with self.assertRaises(SessionError):
            EnvSessionProvider({SESSION_ENV_REQUEST_ID: "   "}).acquire()


class TestFileProvider(unittest.TestCase):
    def _write(self, content: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_reads_json(self) -> None:
        path = self._write(json.dumps({
            "request_id": "rid-2", "cuid": "c-2",
            "host": "https://api.example", "ttl_s": 60}))
        s = FileSessionProvider(path).acquire()
        self.assertEqual(s.request_id, "rid-2")
        self.assertEqual(s.cuid, "c-2")
        self.assertEqual(s.host, "https://api.example")
        self.assertEqual(s.ttl_s, 60.0)

    def test_camel_case_aliases(self) -> None:
        path = self._write(json.dumps({"requestId": "rid-3", "userId": "u-3"}))
        s = FileSessionProvider(path).acquire()
        self.assertEqual(s.request_id, "rid-3")
        self.assertEqual(s.cuid, "u-3")

    def test_missing_file(self) -> None:
        with self.assertRaises(SessionError):
            FileSessionProvider("/nonexistent/session.json").acquire()

    def test_invalid_json(self) -> None:
        path = self._write("{not json")
        with self.assertRaises(SessionError):
            FileSessionProvider(path).acquire()

    def test_json_array_rejected(self) -> None:
        path = self._write("[1,2,3]")
        with self.assertRaises(SessionError):
            FileSessionProvider(path).acquire()

    def test_missing_request_id_field(self) -> None:
        path = self._write(json.dumps({"cuid": "only-cuid"}))
        with self.assertRaises(SessionError):
            FileSessionProvider(path).acquire()

    def test_describe_does_not_leak(self) -> None:
        path = self._write(json.dumps({"request_id": "super-secret-token"}))
        d = FileSessionProvider(path).describe()
        self.assertNotIn("super-secret-token", json.dumps(d))


class TestCommandProvider(unittest.TestCase):
    """登录命令 provider（**无 shell**，argv 列表）。"""

    def test_parses_json_stdout(self) -> None:
        p = CommandSessionProvider(
            ["printf", "%s", '{"request_id":"rid-4","cuid":"c-4"}'])
        s = p.acquire()
        self.assertEqual(s.request_id, "rid-4")
        self.assertEqual(s.cuid, "c-4")

    def test_string_form_is_split_without_shell(self) -> None:
        p = CommandSessionProvider(
            command="printf %s '{\"request_id\":\"rid-str\"}'")
        self.assertEqual(p.acquire().request_id, "rid-str")

    def test_shell_metacharacters_are_not_executed(self) -> None:
        """CWE-78 回归：shell 元字符不得被解释成第二个命令。

        命令以 printf 输出 JSON，其中 request_id 故意包含 `; touch <文件>`。
        若无 shell（本实现），它只能作为普通字符串；若经 shell，则会创建文件。
        """
        marker = tempfile.mkstemp()[1]
        os.unlink(marker)
        self.addCleanup(lambda: os.path.exists(marker) and os.unlink(marker))
        payload = '{"request_id":"x; touch %s"}' % marker
        p = CommandSessionProvider(["printf", "%s", payload])
        out = p.acquire()
        self.assertEqual(out.request_id, "x; touch %s" % marker)
        self.assertFalse(os.path.exists(marker),
                         "shell 元字符被执行了——命令注入！")

    def test_tolerates_log_lines_before_json(self) -> None:
        p = CommandSessionProvider([
            "python3", "-c",
            "print('logging in...');print('{\"request_id\":\"rid-5\"}')",
        ])
        self.assertEqual(p.acquire().request_id, "rid-5")

    def test_nonzero_exit_reports_stderr(self) -> None:
        p = CommandSessionProvider([
            "python3", "-c",
            "import sys;print('boom', file=sys.stderr);sys.exit(3)",
        ])
        with self.assertRaises(SessionError) as ctx:
            p.acquire()
        msg = str(ctx.exception)
        self.assertIn("3", msg)
        self.assertIn("boom", msg)

    def test_empty_stdout_rejected(self) -> None:
        with self.assertRaises(SessionError):
            CommandSessionProvider(["true"]).acquire()

    def test_invalid_json_rejected(self) -> None:
        with self.assertRaises(SessionError):
            CommandSessionProvider(["echo", "not-json"]).acquire()

    def test_json_non_object_rejected(self) -> None:
        with self.assertRaises(SessionError):
            CommandSessionProvider(["echo", "[1,2,3]"]).acquire()

    def test_empty_command_rejected(self) -> None:
        for bad in ([], ["  "]):
            with self.subTest(bad=bad):
                with self.assertRaises(SessionError):
                    CommandSessionProvider(bad)
        for bad in ("", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(SessionError):
                    CommandSessionProvider(command=bad)
        with self.assertRaises(SessionError):
            CommandSessionProvider()

    def test_timeout(self) -> None:
        p = CommandSessionProvider(["sleep", "5"], timeout=0.3)
        with self.assertRaises(SessionError) as ctx:
            p.acquire()
        self.assertIn("超时", str(ctx.exception))

    def test_nonexistent_binary_reports_oserror(self) -> None:
        with self.assertRaises(SessionError) as ctx:
            CommandSessionProvider(["/nonexistent/login-helper"]).acquire()
        self.assertIn("无法执行", str(ctx.exception))

    def test_describe_records_last_error(self) -> None:
        p = CommandSessionProvider(["false"])
        with self.assertRaises(SessionError):
            p.acquire()
        d = p.describe()
        self.assertIn("last_error", d)
        self.assertTrue(d["last_error"])
        self.assertEqual(d["argv"], ["false"])

    def test_command_property_is_readable(self) -> None:
        p = CommandSessionProvider(["bash", "/x/login.sh"])
        self.assertEqual(p.command, "bash /x/login.sh")


class TestChainProvider(unittest.TestCase):
    def test_falls_back_in_order(self) -> None:
        order: List[str] = []

        class P(SessionProvider):
            def __init__(self, name: str, ok: bool) -> None:
                self.name = name
                self.ok = ok

            def acquire(self, previous=None) -> Session:
                order.append(self.name)
                if not self.ok:
                    raise SessionError("%s 不可用" % self.name)
                return Session(request_id="from-%s" % self.name)

        chain = ChainSessionProvider([P("a", False), P("b", False), P("c", True)])
        s = chain.acquire()
        self.assertEqual(s.request_id, "from-c")
        self.assertEqual(order, ["a", "b", "c"])

    def test_all_failed_aggregates(self) -> None:
        class Bad(SessionProvider):
            def __init__(self, n: str) -> None:
                self.name = n

            def acquire(self, previous=None) -> Session:
                raise SessionError("%s 挂了" % self.name)

        with self.assertRaises(SessionError) as ctx:
            ChainSessionProvider([Bad("x"), Bad("y")]).acquire()
        msg = str(ctx.exception)
        self.assertIn("x", msg)
        self.assertIn("y", msg)


class TestNullProvider(unittest.TestCase):
    def test_error_is_actionable(self) -> None:
        with self.assertRaises(SessionError) as ctx:
            NullSessionProvider("未配置").acquire()
        msg = str(ctx.exception)
        # 必须告诉运维怎么补上会话，而不是只报错
        for hint in (SESSION_ENV_COMMAND, SESSION_ENV_FILE, SESSION_ENV_REQUEST_ID):
            self.assertIn(hint, msg)
        self.assertIn("request_id", msg)


class TestMakeProvider(unittest.TestCase):
    def test_priority_command_then_file_then_env(self) -> None:
        p = make_session_provider(env={
            SESSION_ENV_COMMAND: "echo x",
            SESSION_ENV_FILE: "/tmp/s.json",
            SESSION_ENV_REQUEST_ID: "rid",
        })
        self.assertEqual([m["provider"] for m in p.describe()["members"]],
                         ["command", "file", "env"])

    def test_single_provider_not_wrapped(self) -> None:
        p = make_session_provider(env={SESSION_ENV_REQUEST_ID: "rid"})
        self.assertIsInstance(p, EnvSessionProvider)

    def test_empty_env_gives_null(self) -> None:
        self.assertIsInstance(make_session_provider(env={}), NullSessionProvider)

    def test_explicit_arg_beats_env(self) -> None:
        p = make_session_provider(env={SESSION_ENV_REQUEST_ID: "from-env"},
                                  request_id="from-arg")
        self.assertEqual(p.acquire().request_id, "from-arg")


# --------------------------------------------------------------------------- #
# 自动续期（核心回归）
# --------------------------------------------------------------------------- #

class _RotatingProvider(SessionProvider):
    """每次 acquire 都给一个新会话，用于验证续期确实发生了。"""

    name = "rotating"

    def __init__(self) -> None:
        self.acquires = 0
        self.invalidated: List[Optional[Session]] = []

    def acquire(self, previous: Optional[Session] = None) -> Session:
        self.acquires += 1
        return Session(request_id="session-%d" % self.acquires, cuid="cuid-1")

    def invalidate(self, session: Optional[Session]) -> None:
        self.invalidated.append(session)


class TestAutoRefresh(unittest.TestCase):
    """`_call_with_refresh` 的行为：只在鉴权失败时续期。"""

    def _source(self, provider: SessionProvider) -> LEYUSource:
        return LEYUSource(host="https://api.example", session_provider=provider,
                          replay=None, timeout=1.0)

    def test_auth_error_triggers_refresh_and_retry(self) -> None:
        prov = _RotatingProvider()
        src = self._source(prov)
        calls: List[str] = []

        def flaky() -> str:
            calls.append(src.session.request_id)
            if len(calls) == 1:
                raise AuthError("业务返回鉴权失败 code=0401013")
            return "ok"

        self.assertEqual(src._call_with_refresh(flaky), "ok")
        self.assertEqual(len(calls), 2, "必须重试一次")
        # 两次用的必须是**不同**会话 —— 证明真的重新登录了
        self.assertNotEqual(calls[0], calls[1])
        self.assertEqual(prov.acquires, 2)
        self.assertEqual(len(prov.invalidated), 1)

    def test_non_auth_error_does_not_refresh(self) -> None:
        """业务错误（非鉴权）不得触发重新登录，否则会疯狂重登。"""
        prov = _RotatingProvider()
        src = self._source(prov)
        calls: List[int] = []

        def boom() -> None:
            calls.append(1)
            raise DecodeError("业务返回失败 code=0400500 赛事已经结束")

        with self.assertRaises(DecodeError):
            src._call_with_refresh(boom)
        self.assertEqual(len(calls), 1, "非鉴权错误不应重试")
        self.assertEqual(prov.acquires, 0, "不应因业务错误而重新获取会话")
        self.assertEqual(prov.invalidated, [], "不应废弃会话")

    def test_persistent_auth_error_gives_up_after_attempts(self) -> None:
        prov = _RotatingProvider()
        src = self._source(prov)
        calls: List[int] = []

        def always_auth_fail() -> None:
            calls.append(1)
            raise AuthError("code=0401013")

        with self.assertRaises(AuthError):
            src._call_with_refresh(always_auth_fail, attempts=3)
        self.assertEqual(len(calls), 3, "最多尝试 attempts 次")
        # 首个会话在重试前已取回，因此只有「剩余 attempts 次」才会计数。
        # 关键不变量：重试次数 == 刷新次数 + 1
        self.assertEqual(prov.acquires, len(calls) - 1,
                         "每次重试前必须刷新一次会话")

    def test_session_lazily_acquired_only_once(self) -> None:
        prov = _RotatingProvider()
        src = self._source(prov)
        self.assertEqual(prov.acquires, 0, "构造时不应登录")
        _ = src.session
        _ = src.session
        self.assertEqual(prov.acquires, 1, "未过期时复用同一会话")

    def test_expired_session_is_reacquired(self) -> None:
        class ShortLived(_RotatingProvider):
            def acquire(self, previous=None) -> Session:
                self.acquires += 1
                return Session(request_id="s-%d" % self.acquires, ttl_s=1e-9)

        prov = ShortLived()
        src = self._source(prov)
        _ = src.session
        _ = src.session
        self.assertEqual(prov.acquires, 2, "过期后应重新获取")

    def test_client_is_rebuilt_with_new_session(self) -> None:
        """续期后客户端必须带上新 requestId，否则重试还是用旧令牌。"""
        prov = _RotatingProvider()
        src = self._source(prov)
        first = src.client.request_id
        src._refresh_session()
        self.assertNotEqual(src.client.request_id, first)
        self.assertEqual(src.client.request_id, "session-2")

    def test_describe_reports_session_state(self) -> None:
        prov = _RotatingProvider()
        src = self._source(prov)
        _ = src.session
        d = src.describe()
        self.assertEqual(d["session"]["provider"], "rotating")
        self.assertGreaterEqual(d["session_refreshes"], 1)
        # 不得泄漏令牌
        self.assertNotIn("session-1", json.dumps(d))

    def test_missing_session_surfaces_actionable_error(self) -> None:
        src = LEYUSource(host="https://api.example",
                         session_provider=NullSessionProvider("测试"))
        with self.assertRaises(SessionError):
            src.schedule()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCachedSessionProvider(unittest.TestCase):
    """会话缓存：主来源失败时回退（防「App token 一失效就全停采集」）。"""

    def setUp(self) -> None:
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "_session.json")

    def _seed(self, rid: str = "cached-rid-1") -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"request_id": rid, "host": "https://api.example"},
                      fh)

    def test_falls_back_to_cache_when_primary_fails(self) -> None:
        from collector.session import CachedSessionProvider

        class Bad(SessionProvider):
            name = "bad"

            def acquire(self, previous=None):
                raise SessionError("6001 token已过期")

        self._seed("cached-rid-1")
        s = CachedSessionProvider(Bad(), self.path).acquire()
        self.assertEqual(s.request_id, "cached-rid-1")
        self.assertIn("缓存", s.note)

    def test_saves_on_success(self) -> None:
        from collector.session import CachedSessionProvider

        class Ok(SessionProvider):
            name = "ok"

            def acquire(self, previous=None):
                return Session(request_id="fresh-rid")

        CachedSessionProvider(Ok(), self.path).acquire()
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["request_id"], "fresh-rid")

    def test_raises_when_both_fail(self) -> None:
        from collector.session import CachedSessionProvider

        class Bad(SessionProvider):
            name = "bad"

            def acquire(self, previous=None):
                raise SessionError("boom")

        with self.assertRaises(SessionError) as ctx:
            CachedSessionProvider(Bad(), self.path).acquire()
        # 报错必须可操作（指向要更新的变量与缓存路径）
        self.assertIn("LEYU_APP_TOKEN", str(ctx.exception))

    def test_invalidate_removes_cache(self) -> None:
        from collector.session import CachedSessionProvider

        class Ok(SessionProvider):
            name = "ok"

            def acquire(self, previous=None):
                return Session(request_id="r")

        p = CachedSessionProvider(Ok(), self.path)
        p.acquire()
        self.assertTrue(os.path.exists(self.path))
        p.invalidate(None)
        self.assertFalse(os.path.exists(self.path))

    def test_corrupt_cache_ignored(self) -> None:
        from collector.session import CachedSessionProvider

        class Bad(SessionProvider):
            name = "bad"

            def acquire(self, previous=None):
                raise SessionError("boom")

        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        with self.assertRaises(SessionError):
            CachedSessionProvider(Bad(), self.path).acquire()

    def test_chain_wraps_cache_even_without_sources(self) -> None:
        """关键回归：即使没有任何会话来源，也应包缓存以便回退。

        早期实现会在无来源时直接返回 NullSessionProvider，
        导致「App 凭据一失效就全停采集」，而缓存里其实还有可用会话。
        """
        from collector.session import CachedSessionProvider
        self._seed("from-cache")
        p = make_session_provider(env={"LEYU_SESSION_CACHE": self.path})
        self.assertIsInstance(p, CachedSessionProvider)
        self.assertEqual(p.acquire().request_id, "from-cache")

    def test_no_cache_env_keeps_plain_chain(self) -> None:
        from collector.session import EnvSessionProvider
        p = make_session_provider(env={"LEYU_REQUEST_ID": "rid"})
        self.assertIsInstance(p, EnvSessionProvider)


class TestCookieOnlySession(unittest.TestCase):
    """只给 cookie 也应能建会话。

    真实限制：早期 `Session.__post_init__` 强制要求 request_id，
    `EnvSessionProvider` 同上，导致「只注入 cookie」被直接拒绝 ——
    而服务端本身也支持 cookie 鉴权/路由（X-API-TOKEN + route）。
    """

    def test_session_accepts_cookie_only(self) -> None:
        s = Session(request_id="", cookie="X-API-TOKEN=abc")
        self.assertEqual(s.request_id, "")
        self.assertEqual(s.cookie, "X-API-TOKEN=abc")

    def test_session_rejects_both_empty(self) -> None:
        for rid, ck in (("", ""), ("  ", "   ")):
            with self.subTest(rid=rid, ck=ck):
                with self.assertRaises(SessionError):
                    Session(request_id=rid, cookie=ck)

    def test_env_provider_cookie_only(self) -> None:
        p = EnvSessionProvider({"LEYU_COOKIE": "route=xyz"})
        s = p.acquire()
        self.assertEqual(s.cookie, "route=xyz")

    def test_env_provider_both_missing_is_actionable(self) -> None:
        with self.assertRaises(SessionError) as ctx:
            EnvSessionProvider({}).acquire()
        msg = str(ctx.exception)
        self.assertIn("LEYU_REQUEST_ID", msg)
        self.assertIn("LEYU_COOKIE", msg)

    def test_cookie_carried_into_client(self) -> None:
        """回归：cookie 必须从 Session 透传到 HTTP 客户端（否则等于没注入）。"""
        from collector.leyu_client import LEYUClient
        c = LEYUClient(host="https://x.test", cookie="route=abc")
        self.assertIn("Cookie", c._headers(False))
