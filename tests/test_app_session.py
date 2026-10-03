#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""乐鱼 App 会话引导链路（`x-api-token` → `requestId`）的单元测试。

这是打通真实数据源的关键一环：业务 API 需要的 `requestId` 是通过
**启动场馆**（`/game/api/v1/venue/launch`）换来的。

覆盖：
  - 凭据对象与脱敏（绝不泄漏 token）
  - 缺少签名时必须给出**可操作**报错，而不是静默失败
  - 场馆业务码分支：6000 成功 / 6005 未开放 / 6415 维护 / 6003 非法请求
  - 响应缺 `url`、`url` 缺 `token` 的降级
  - 网关 scheme 白名单（防 file:// 之类被配置注入）
  - 与 `collector.sources` 的 provider 链集成（含失效自动重建）

真实网络用例默认跳过（需 `AI_FOOTBALL_ONLINE=1`）。

    python3 -m unittest tests.test_app_session -v
"""

from __future__ import annotations

import json
import os
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from collector.leyu_app_session import (
    APP_ENV_HOST,
    APP_ENV_SIGNATURE,
    APP_ENV_TOKEN,
    APP_ENV_UUID,
    DEFAULT_APP_HOST,
    HEADER_PREFIX_SIGNATURE,
    VENUE_OBTY,
    VENUE_YBTY,
    AppCredentials,
    AppSessionBootstrapper,
    AppSessionProvider,
    bootstrapper_from_env,
)
from collector.session import (
    SESSION_ENV_APP_SIGNATURE,
    SESSION_ENV_APP_TOKEN,
    SESSION_ENV_APP_UUID,
    Session,
    SessionError,
    SessionProvider,
    make_session_provider,
)

SIG = "a" * 64  # 合成签名（非真实值）


def _bs(sig: str = SIG, **kw: Any) -> AppSessionBootstrapper:
    return AppSessionBootstrapper(
        AppCredentials("t" * 96, "u" * 16),
        signature=sig, **kw)


def _launch_reply(status: Any) -> Dict[str, Any]:
    return {
        "data": {"url": "https://api.example.test?token=deadbeef",
                 "h5Url": "https://h5.example.test?token=deadbeef"},
        "message": "场馆启动成功",
        "status_code": status,
    }


# --------------------------------------------------------------------------- #
# 凭据
# --------------------------------------------------------------------------- #

class TestCredentials(unittest.TestCase):
    def test_rejects_missing_fields(self) -> None:
        for tok, uid in (("", "u"), ("t", ""), ("  ", "u")):
            with self.subTest(tok=tok, uid=uid):
                with self.assertRaises(SessionError):
                    AppCredentials(tok, uid)

    def test_masked_never_exposes_full_token(self) -> None:
        c = AppCredentials("s" * 96, "u" * 16)
        m = c.masked()
        self.assertNotIn("s" * 96, m)
        self.assertIn("ssssss", m)

    def test_repr_is_masked(self) -> None:
        c = AppCredentials("s" * 96, "u" * 16)
        self.assertNotIn("s" * 96, repr(c))


# --------------------------------------------------------------------------- #
# 引导流程
# --------------------------------------------------------------------------- #

class TestBootstrapper(unittest.TestCase):
    def test_success_extracts_request_id_and_host(self) -> None:
        bs = _bs()
        with mock.patch.object(bs, "_post", return_value=_launch_reply(6000)):
            s = bs.acquire()
        self.assertEqual(s.request_id, "deadbeef")
        self.assertEqual(s.host, "https://api.example.test")
        self.assertGreater(s.ttl_s, 0)
        self.assertIn("场馆", s.note)

    def test_request_id_is_the_url_token(self) -> None:
        """回归：requestId 必须取自 launch 返回 url 的 token 参数。"""
        bs = _bs()
        reply = {"data": {"url": "https://gw.test/x?token=THE_RID&gr=b"},
                 "status_code": 6000}
        with mock.patch.object(bs, "_post", return_value=reply):
            self.assertEqual(bs.acquire().request_id, "THE_RID")

    def test_accepts_string_status_code(self) -> None:
        bs = _bs()
        with mock.patch.object(bs, "_post", return_value=_launch_reply("6000")):
            self.assertEqual(bs.acquire().request_id, "deadbeef")

    def test_maintenance_raises_actionable(self) -> None:
        bs = _bs()
        reply = {"data": {}, "message": "场馆已临时维护", "status_code": 6415}
        with mock.patch.object(bs, "_post", return_value=reply):
            with self.assertRaises(SessionError) as ctx:
                bs.acquire()
        msg = str(ctx.exception)
        self.assertIn("6415", msg)
        self.assertIn(APP_ENV_TOKEN, msg)   # 告诉运维怎么补凭据
        self.assertIn(VENUE_YBTY, msg)

    def test_not_open_yet_raises(self) -> None:
        bs = _bs(venue=VENUE_OBTY)
        with mock.patch.object(bs, "_post",
                              return_value={"data": {}, "status_code": 6005,
                                            "message": "即将开放，敬请期待"}):
            with self.assertRaises(SessionError):
                bs.acquire()

    def test_illegal_request_6003_raises(self) -> None:
        bs = _bs()
        with mock.patch.object(bs, "_post",
                              return_value={"data": {}, "status_code": 6003,
                                            "message": "非法请求"}):
            with self.assertRaises(SessionError) as ctx:
                bs.acquire()
        self.assertIn("6003", str(ctx.exception))

    def test_missing_url_raises(self) -> None:
        bs = _bs()
        with mock.patch.object(bs, "_post",
                              return_value={"data": {"h5Url": "x"},
                                            "status_code": 6000}):
            with self.assertRaises(SessionError) as ctx:
                bs.acquire()
        self.assertIn("url", str(ctx.exception))

    def test_url_without_token_raises(self) -> None:
        bs = _bs()
        with mock.patch.object(bs, "_post",
                              return_value={"data": {"url": "https://gw.test/x?gr=b"},
                                            "status_code": 6000}):
            with self.assertRaises(SessionError) as ctx:
                bs.acquire()
        self.assertIn("token", str(ctx.exception))

    def test_non_mapping_data_raises(self) -> None:
        bs = _bs()
        with mock.patch.object(bs, "_post",
                              return_value={"data": [], "status_code": 6000}):
            with self.assertRaises(SessionError):
                bs.acquire()

    def test_launch_body_uses_configured_venue(self) -> None:
        bs = _bs(venue="ZZZZ")
        seen: List[Dict[str, Any]] = []

        def fake_post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
            seen.append({"path": path, "body": dict(body)})
            return _launch_reply(6000)

        with mock.patch.object(bs, "_post", side_effect=fake_post):
            bs.acquire()
        self.assertEqual(seen[0]["path"], "/game/api/v1/venue/launch")
        self.assertEqual(seen[0]["body"]["enName"], "ZZZZ")
        self.assertEqual(seen[0]["body"]["siteId"], "2001")

    def test_describe_masks_credentials(self) -> None:
        bs = _bs()
        d = bs.describe()
        self.assertEqual(d["venue"], VENUE_YBTY)
        self.assertNotIn("t" * 96, json.dumps(d))


# --------------------------------------------------------------------------- #
# 签名与网关安全
# --------------------------------------------------------------------------- #

class TestSignatureAndHost(unittest.TestCase):
    def test_missing_signature_is_actionable(self) -> None:
        """签名缺失必须给出可操作报错，且不得发起请求。"""
        bs = _bs(sig="")
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(SessionError) as ctx:
                bs.acquire()
        urlopen.assert_not_called()
        msg = str(ctx.exception)
        self.assertIn(HEADER_PREFIX_SIGNATURE, msg)
        self.assertIn(APP_ENV_SIGNATURE, msg)
        # 必须解释这是什么、怎么拿
        self.assertIn("站点级", msg)

    def test_headers_carry_required_fields(self) -> None:
        bs = _bs()
        h = bs._headers()
        for k in ("x-api-client", "x-api-version", "x-api-site",
                  "x-api-uuid", "x-api-token", HEADER_PREFIX_SIGNATURE):
            self.assertIn(k, h)
        self.assertEqual(h[HEADER_PREFIX_SIGNATURE], SIG)

    def test_host_must_be_http_or_https(self) -> None:
        """防配置注入：file:// 之类必须被拒。"""
        for bad in ("file:///etc/passwd", "ftp://x.test", "gopher://x"):
            with self.subTest(bad=bad):
                with self.assertRaises(SessionError):
                    _bs(app_host=bad)

    def test_host_trailing_slash_normalised(self) -> None:
        bs = _bs(app_host="https://h.test/")
        self.assertEqual(bs.app_host, "https://h.test")


# --------------------------------------------------------------------------- #
# provider 集成
# --------------------------------------------------------------------------- #

class TestProviderIntegration(unittest.TestCase):
    def test_is_a_session_provider(self) -> None:
        self.assertIsInstance(AppSessionProvider(_bs()), SessionProvider)

    def test_counts_refreshes(self) -> None:
        prov = AppSessionProvider(_bs())
        with mock.patch.object(prov.bootstrapper, "acquire",
                               return_value=Session(request_id="r")):
            prov.acquire()
            prov.acquire()
        self.assertEqual(prov.refreshes, 2)

    def test_env_requires_all_three_values(self) -> None:
        """三个变量缺任一都不启用 App 引导（避免半配置下静默失败）。"""
        full = {APP_ENV_TOKEN: "t", APP_ENV_UUID: "u", APP_ENV_SIGNATURE: SIG}
        self.assertIsNotNone(bootstrapper_from_env(full))
        for missing in (APP_ENV_TOKEN, APP_ENV_UUID, APP_ENV_SIGNATURE):
            partial = {k: v for k, v in full.items() if k != missing}
            with self.subTest(missing=missing):
                self.assertIsNone(bootstrapper_from_env(partial))

    def test_env_app_host_override(self) -> None:
        bs = bootstrapper_from_env({APP_ENV_TOKEN: "t", APP_ENV_UUID: "u",
                                    APP_ENV_SIGNATURE: SIG,
                                    APP_ENV_HOST: "https://custom.test"})
        assert bs is not None
        self.assertEqual(bs.app_host, "https://custom.test")

    def test_env_default_host(self) -> None:
        bs = bootstrapper_from_env({APP_ENV_TOKEN: "t", APP_ENV_UUID: "u",
                                    APP_ENV_SIGNATURE: SIG})
        assert bs is not None
        self.assertEqual(bs.app_host, DEFAULT_APP_HOST)

    def test_provider_chain_prefers_app_first(self) -> None:
        """App 引导应在链首：不依赖人工登录，且可自动续期。"""
        env = {
            SESSION_ENV_APP_TOKEN: "t", SESSION_ENV_APP_UUID: "u",
            SESSION_ENV_APP_SIGNATURE: SIG,
            "LEYU_SESSION_FILE": "/tmp/s.json",
            "LEYU_REQUEST_ID": "rid",
        }
        p = make_session_provider(env=env)
        names = [m["provider"] for m in p.describe()["members"]]
        self.assertEqual(names[0], "app-launch")
        self.assertIn("file", names)
        self.assertIn("env", names)

    def test_app_can_be_disabled(self) -> None:
        env = {SESSION_ENV_APP_TOKEN: "t", SESSION_ENV_APP_UUID: "u",
               SESSION_ENV_APP_SIGNATURE: SIG}
        p = make_session_provider(env=env, include_app=False)
        self.assertNotIn("app-launch", json.dumps(p.describe()))

    def test_chain_falls_back_when_launch_fails(self) -> None:
        """场馆维护时应回退到后面的来源，而不是整体失败。"""
        env = {
            SESSION_ENV_APP_TOKEN: "t", SESSION_ENV_APP_UUID: "u",
            SESSION_ENV_APP_SIGNATURE: SIG,
            "LEYU_REQUEST_ID": "fallback-rid",
        }
        p = make_session_provider(env=env)
        with mock.patch.object(AppSessionBootstrapper, "acquire",
                               side_effect=SessionError("场馆已临时维护")):
            s = p.acquire()
        self.assertEqual(s.request_id, "fallback-rid")


# --------------------------------------------------------------------------- #
# 真实网络（默认跳过）
# --------------------------------------------------------------------------- #

@unittest.skipUnless(os.environ.get("AI_FOOTBALL_ONLINE") == "1",
                     "在线用例默认跳过（设 AI_FOOTBALL_ONLINE=1 开启）")
class TestOnlineReality(unittest.TestCase):
    """真实环境：App 凭据 → requestId → 业务 API 取到真实数据。"""

    @classmethod
    def setUpClass(cls) -> None:
        boot = bootstrapper_from_env()
        if boot is None:
            raise unittest.SkipTest(
                "需要 LEYU_APP_TOKEN / LEYU_APP_UUID / LEYU_APP_SIGNATURE")
        cls.bs = boot

    def test_acquire_returns_usable_session(self) -> None:
        s = self.bs.acquire()
        self.assertTrue(s.request_id)
        self.assertTrue(s.host.startswith("http"))

    def test_source_fetches_real_odds(self) -> None:
        from collector.sources import LEYUSource
        src = LEYUSource(session_provider=AppSessionProvider(self.bs))
        snaps, issues = src.fetch(max_matches=5)
        self.assertEqual(issues, [])
        self.assertGreater(len(snaps), 0)
        self.assertEqual({s.source for s in snaps}, {"乐鱼API"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
