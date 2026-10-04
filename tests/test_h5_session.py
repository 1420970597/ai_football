#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""乐鱼 H5 cookie 会话引导链路的单元测试。

本模块把「浏览器打开乐鱼网页版时做的事」自动化了：

    1. 带 Cookie 取 `/api/json-cache/y-h5-main:leyu:prod:liveDomain`
       → AES-128-ECB 解密 → 网关候选列表
    2. `POST /game/api/v1/venue/launch`（Cookie + `x-api-token` + `x-api-xxx`）
       → `data.url` 的 `token=` 即 `requestId`
       → `data.h5Url` 的 `?api=` 解密后即**业务网关**

覆盖：
  - Cookie 解析（整串 / 分字段 / 重复名后者覆盖）
  - 凭据脱敏（绝不泄漏完整 token）
  - `liveDomain` 解密（含 `+` 被 URL 解码吃掉的真实踩坑回归）
  - `api=` 网关解密（key = OBTY20220712OBTY）
  - 业务码分支：6000 成功 / 6001 token 过期 / 6003 非法请求
  - 缺 site / 缺签名 / 空 liveDomain 时的**可操作**报错
  - 网关 scheme 白名单（防 file:// 配置注入）
  - provider 链：H5 优先、失败回退、计数续期

真实网络用例默认跳过（需 `AI_FOOTBALL_ONLINE=1`）。

    python3 -m unittest tests.test_h5_session -v
"""

from __future__ import annotations

import json
import os
import unittest
from typing import Any, Dict
from unittest import mock

from collector.leyu_client import API_AES_KEY, decrypt_aes_ecb
from collector.leyu_h5_session import (
    H5_CACHE_AES_KEY,
    H5_ENV_COOKIE,
    H5_ENV_SIGNATURE,
    H5_ENV_SITE,
    H5_ENV_TOKEN,
    H5_ENV_UUID,
    H5Credentials,
    H5SessionBootstrapper,
    H5SessionProvider,
    h5_bootstrapper_from_env,
    parse_cookie,
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

SIG = "a" * 64          # 合成签名（非真实值）
SITE = "https://h5.example.test:6510"
TOKEN = "f" * 96        # 合成 token（非真实值）
UUID = "11111111-2222-4333-8444-555555555555"

#: 合成业务网关
GW = "https://api.example-gw.test"

# --------------------------------------------------------------------------- #
# 密文夹具（**ground truth 来自 openssl**，不是本项目自己加密再自己解密）
#
# 生成方式（可复现）：
#   printf '%s' '<plain>' | openssl enc -aes-128-ecb -nopad -K <key.hex()> | base64
# 并在本文件下方 `test_fixtures_match_openssl_ground_truth` 中校验
# `decrypt_aes_ecb()` 与 openssl 逐字节一致，防止「自证循环」。
# --------------------------------------------------------------------------- #

#: `?api=` 网关密文 → 明文（key = API_AES_KEY = OBTY20220712OBTY）
API_CASES = [
    ("HKI1zEwtne1n2bQ3/7He8UHpA+9ipFt5nvWFGuAsSFY=", GW),
    # 含 `+` 的用例：`parse_qs` 会把 `+` 解成空格，必须原样取值
    ("HKI1zEwtne1n2bQ3/7He8YM36W+J8q2aehaggzPZDhA=", GW + "?s=1"),
    ("HKI1zEwtne1n2bQ3/7He8ellHTGjFtR8Km6D7X9+cfI=", GW + "?s=7"),
]

#: json-cache 响应体密文 → 明文（key = H5_CACHE_AES_KEY = A-DB/(z$/?13cfae）
CACHE_CASES = [
    ("Ae9HcFoayN74FCVPb4U5vEOWENXZpLCu2ybRDvTeHNaDIFN7FeiiTWSoQNZ37q/u",
     '{"url":["https://bk1.test","https://bk2.test"]}'),
    ("ERjnqnl+v/jdEzvlz0p510Kco3Wc+tb2cYVfekBXdbo=",
     '{"url":"https://bk.test"}'),
]


def _creds() -> H5Credentials:
    return H5Credentials(TOKEN, UUID)


def _bs(sig: str = SIG, site: str = SITE, **kw: Any) -> H5SessionBootstrapper:
    return H5SessionBootstrapper(_creds(), signature=sig, site=site, **kw)


def _launch_reply(status: Any = 6000, api_cipher: str = API_CASES[0][0],
                  token: str = "THE_RID") -> Dict[str, Any]:
    """构造一次成功的 launch 响应（含加密的 api= 网关）。"""
    import urllib.parse
    api = urllib.parse.quote(api_cipher, safe="")
    url = ("https://app-h5.example.test?token=%s&gr=b&lg=zh&api=%s"
           % (token, api))
    return {"data": {"url": url, "h5Url": url,
                     "resource": "https://img.test"}, "status_code": status}


# --------------------------------------------------------------------------- #
# Cookie 解析
# --------------------------------------------------------------------------- #

class TestCookieParsing(unittest.TestCase):
    def test_parses_named_pairs(self) -> None:
        jar = parse_cookie("a=1; b=2; c=3")
        self.assertEqual(jar, {"a": "1", "b": "2", "c": "3"})

    def test_ignores_chunks_without_equals(self) -> None:
        self.assertEqual(parse_cookie("garbage; a=1; ; b=2"),
                         {"a": "1", "b": "2"})

    def test_duplicate_name_last_wins(self) -> None:
        """与浏览器一致：同名 cookie 后者覆盖前者。"""
        self.assertEqual(parse_cookie("k=old; k=new"), {"k": "new"})

    def test_empty_string(self) -> None:
        self.assertEqual(parse_cookie(""), {})

    def test_from_cookie_builds_credentials(self) -> None:
        c = H5Credentials.from_cookie(
            "X-API-UUID=%s; X-API-TOKEN=%s; theme=light" % (UUID, TOKEN))
        self.assertEqual(c.token, TOKEN)
        self.assertEqual(c.uuid, UUID)
        # 其余 cookie 应保留（浏览器会一起发送）
        self.assertIn("theme=light", c.cookie_header())
        # 但不应重复出现 token / uuid
        self.assertEqual(c.cookie_header().count("X-API-TOKEN="), 1)
        self.assertEqual(c.cookie_header().count("X-API-UUID="), 1)

    def test_from_cookie_missing_token_raises(self) -> None:
        with self.assertRaises(SessionError):
            H5Credentials.from_cookie("theme=light")

    def test_uuid_falls_back_to_placeholder(self) -> None:
        """实测服务端不校验 uuid 与 token 的绑定，缺失时用占位即可。"""
        c = H5Credentials(TOKEN)
        self.assertTrue(c.uuid)
        self.assertIn("X-API-UUID=", c.cookie_header())


# --------------------------------------------------------------------------- #
# 凭据脱敏
# --------------------------------------------------------------------------- #

class TestCredentialMasking(unittest.TestCase):
    def test_rejects_empty_token(self) -> None:
        for bad in ("", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(SessionError):
                    H5Credentials(bad)

    def test_masked_never_exposes_full_token(self) -> None:
        m = _creds().masked()
        self.assertNotIn(TOKEN, m)
        self.assertIn("ffffff", m)

    def test_repr_is_masked(self) -> None:
        self.assertNotIn(TOKEN, repr(_creds()))

    def test_error_message_names_env_vars(self) -> None:
        """报错必须告诉运维去哪里补凭据。"""
        with self.assertRaises(SessionError) as ctx:
            H5Credentials("")
        self.assertIn(H5_ENV_TOKEN, str(ctx.exception))


# --------------------------------------------------------------------------- #
# liveDomain（第一跳）
# --------------------------------------------------------------------------- #

class TestLiveDomains(unittest.TestCase):
    def _reply(self, cipher: str) -> bytes:
        return json.dumps(cipher).encode("utf-8")

    def test_fixtures_match_openssl_ground_truth(self) -> None:
        """防「自证循环」：密文夹具必须能被本项目的解密器还原。

        夹具由 openssl 生成（生成命令见文件头注释），
        此用例同时校验 json-cache 与 api= 两组密钥。
        """
        for cipher, plain in CACHE_CASES:
            with self.subTest(cipher=cipher[:12]):
                self.assertEqual(decrypt_aes_ecb(cipher, H5_CACHE_AES_KEY), plain)
        for cipher, plain in API_CASES:
            with self.subTest(cipher=cipher[:12]):
                self.assertEqual(decrypt_aes_ecb(cipher, API_AES_KEY), plain)

    def test_decrypts_gateway_list(self) -> None:
        bs = _bs()
        with mock.patch.object(
                bs, "_open",
                return_value=(200, self._reply(CACHE_CASES[0][0]), "json")):
            self.assertEqual(bs.fetch_live_domains(),
                             ["https://bk1.test", "https://bk2.test"])

    def test_accepts_single_string_url(self) -> None:
        bs = _bs()
        with mock.patch.object(
                bs, "_open",
                return_value=(200, self._reply(CACHE_CASES[1][0]), "json")):
            self.assertEqual(bs.fetch_live_domains(), ["https://bk.test"])

    def test_empty_body_means_cookie_expired(self) -> None:
        """实测：cookie 失效时该端点返回 HTTP 200 + 空串（不是错误码）。

        必须转成**可操作**报错，而不是静默返回空列表。
        """
        bs = _bs()
        with mock.patch.object(bs, "_open", return_value=(200, b'""', "json")):
            with self.assertRaises(SessionError) as ctx:
                bs.fetch_live_domains()
        msg = str(ctx.exception)
        self.assertIn("失效", msg)
        self.assertIn(H5_ENV_TOKEN, msg)     # 告诉运维怎么补
        self.assertIn("登录", msg)

    def test_non_json_body_raises(self) -> None:
        bs = _bs()
        with mock.patch.object(bs, "_open",
                              return_value=(200, b"<html>oops</html>", "text/html")):
            with self.assertRaises(SessionError):
                bs.fetch_live_domains()

    def test_corrupt_cipher_raises(self) -> None:
        """密文不是合法 AES 块时必须报错，不能静默返回空。"""
        bs = _bs()
        with mock.patch.object(bs, "_open",
                              return_value=(200, b'"not-base64!!"', "json")):
            with self.assertRaises(SessionError):
                bs.fetch_live_domains()

    def test_url_contains_the_cache_key(self) -> None:
        bs = _bs()
        seen: Dict[str, str] = {}

        def _cap(req: Any) -> Any:
            seen["url"] = req.full_url
            return (200, json.dumps(CACHE_CASES[1][0]).encode(), "json")

        with mock.patch.object(bs, "_open", side_effect=_cap):
            bs.fetch_live_domains()
        # 缓存键含 `:`，在 URL 路径里会被百分号编码，故按解码后比对
        import urllib.parse
        path = urllib.parse.unquote(urllib.parse.urlsplit(seen["url"]).path)
        self.assertIn("json-cache", path)
        self.assertIn("y-h5-main:leyu:prod:liveDomain", path)


# --------------------------------------------------------------------------- #
# api= 网关解密（关键回归）
# --------------------------------------------------------------------------- #

class TestApiHostDecryption(unittest.TestCase):
    def test_decrypts_gateway(self) -> None:
        self.assertEqual(_bs()._api_host(_launch_reply()["data"]["url"]), GW)

    def test_plus_sign_in_cipher_survives(self) -> None:
        """**真实踩坑回归**：上游返回的 `api=` 密文里含**未编码的** `+`
        （实测形如 `…m+ySS5gr6Uqu7A=`），而 `parse_qs` 会把裸 `+`
        解成空格，导致 AES 解密静默失败。必须原样取值。

        同时反向断言「旧写法（parse_qs）确实会解错」，
        证明本回归用例真的能拦住该 bug。
        """
        import urllib.parse
        cipher, expected = API_CASES[1]
        self.assertIn("+", cipher)             # 确保覆盖到该分支
        # 关键：与上游一致，**不**把 `+` 编码成 `%2B`
        url = "https://app-h5.test?token=R&api=%s" % cipher
        self.assertEqual(_bs()._api_host(url), expected)
        # 反向证明：parse_qs 会把裸 `+` 解成空格（这正是当初的 bug）
        broken = urllib.parse.parse_qs(
            urllib.parse.urlsplit(url).query)["api"][0]
        self.assertNotEqual(broken, cipher)
        self.assertIn(" ", broken)

    def test_missing_api_param_returns_empty(self) -> None:
        self.assertEqual(_bs()._api_host("https://app-h5.test?token=R"), "")

    def test_corrupt_api_param_returns_empty_not_raise(self) -> None:
        """网关解密属增强信息，失败不应阻断会话获取。"""
        bs = _bs()
        self.assertEqual(bs._api_host("https://app-h5.test?api=bogus"), "")
        self.assertTrue(bs.last_error)


# --------------------------------------------------------------------------- #
# launch / acquire
# --------------------------------------------------------------------------- #

class TestBootstrapper(unittest.TestCase):
    def test_acquire_extracts_request_id_host_origin(self) -> None:
        bs = _bs()
        with mock.patch.object(bs, "_open",
                              return_value=(200, json.dumps(_launch_reply()).encode(),
                                            "json")):
            s = bs.acquire()
        self.assertEqual(s.request_id, "THE_RID")
        self.assertEqual(s.host, GW)                       # 业务网关
        self.assertEqual(s.origin, "https://app-h5.example.test")
        self.assertGreater(s.ttl_s, 0)

    def test_cookie_is_carried_into_session(self) -> None:
        """与浏览器一致：业务请求也应带 cookie（网关可能用于路由/风控）。"""
        bs = _bs()
        with mock.patch.object(bs, "_open",
                              return_value=(200, json.dumps(_launch_reply()).encode(),
                                            "json")):
            s = bs.acquire()
        self.assertIn("X-API-TOKEN=" + TOKEN, s.cookie)

    def test_accepts_string_status_code(self) -> None:
        bs = _bs()
        with mock.patch.object(bs, "_open",
                              return_value=(200, json.dumps(
                                  _launch_reply("6000")).encode(), "json")):
            self.assertEqual(bs.acquire().request_id, "THE_RID")

    def test_expired_token_raises_actionable(self) -> None:
        bs = _bs()
        reply = {"data": {}, "message": "token已过期", "status_code": 6001}
        with mock.patch.object(bs, "_open",
                              return_value=(200, json.dumps(reply).encode(), "json")):
            with self.assertRaises(SessionError) as ctx:
                bs.acquire()
        msg = str(ctx.exception)
        self.assertIn("6001", msg)
        self.assertIn(H5_ENV_TOKEN, msg)      # 怎么补 cookie
        self.assertIn("登录", msg)

    def test_illegal_request_6003_names_signature_var(self) -> None:
        bs = _bs()
        reply = {"data": {}, "message": "非法请求", "status_code": 6003}
        with mock.patch.object(bs, "_open",
                              return_value=(200, json.dumps(reply).encode(), "json")):
            with self.assertRaises(SessionError) as ctx:
                bs.acquire()
        self.assertIn(H5_ENV_SIGNATURE, str(ctx.exception))

    def test_missing_signature_is_actionable(self) -> None:
        bs = H5SessionBootstrapper(_creds(), signature="", site=SITE)
        with self.assertRaises(SessionError) as ctx:
            bs.launch()
        msg = str(ctx.exception)
        self.assertIn("x-api-xxx", msg)
        self.assertIn("站点级", msg)          # 解释该值的性质
        self.assertIn("缓存", msg)            # 说明可长期复用

    def test_missing_site_is_actionable(self) -> None:
        bs = H5SessionBootstrapper(_creds(), signature=SIG, site="")
        with self.assertRaises(SessionError) as ctx:
            bs.launch()
        self.assertIn(H5_ENV_SITE, str(ctx.exception))

    def test_url_without_token_raises(self) -> None:
        bs = _bs()
        reply = {"data": {"url": "https://app-h5.test/no-token"},
                 "status_code": 6000}
        with mock.patch.object(bs, "_open",
                              return_value=(200, json.dumps(reply).encode(), "json")):
            with self.assertRaises(SessionError):
                bs.acquire()

    def test_host_must_be_http_or_https(self) -> None:
        """防配置注入：file:// 之类必须被拒。"""
        for bad in ("file:///etc/passwd", "ftp://x.test", "gopher://x"):
            with self.subTest(bad=bad):
                with self.assertRaises(SessionError):
                    _bs(site=bad)

    def test_site_trailing_slash_normalised(self) -> None:
        self.assertEqual(_bs(site=SITE + "/").site, SITE)

    def test_describe_masks_credentials(self) -> None:
        d = _bs().describe()
        self.assertNotIn(TOKEN, json.dumps(d))
        self.assertTrue(d["signature_set"])
        self.assertEqual(d["bootstrapper"], "h5-cookie")

    def test_launch_request_carries_cookie_and_signature(self) -> None:
        bs = _bs()
        seen: Dict[str, Any] = {}

        def _cap(req: Any) -> Any:
            seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
            seen["url"] = req.full_url
            return (200, json.dumps(_launch_reply()).encode(), "json")

        with mock.patch.object(bs, "_open", side_effect=_cap):
            bs.launch()
        self.assertIn("venue/launch", seen["url"])
        self.assertEqual(seen["headers"]["x-api-client"], "h5")
        self.assertEqual(seen["headers"]["x-api-token"], TOKEN)
        self.assertEqual(seen["headers"]["x-api-xxx"], SIG)
        self.assertIn("X-API-TOKEN=" + TOKEN, seen["headers"]["cookie"])

    def test_venue_override(self) -> None:
        bs = _bs(venue="DBTY")
        seen: Dict[str, Any] = {}

        def _cap(req: Any) -> Any:
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return (200, json.dumps(_launch_reply()).encode(), "json")

        with mock.patch.object(bs, "_open", side_effect=_cap):
            bs.launch()
        self.assertEqual(seen["body"]["enName"], "DBTY")


# --------------------------------------------------------------------------- #
# provider 集成
# --------------------------------------------------------------------------- #

class TestProviderIntegration(unittest.TestCase):
    def test_is_a_session_provider(self) -> None:
        self.assertIsInstance(H5SessionProvider(_bs()), SessionProvider)

    def test_counts_refreshes(self) -> None:
        """续期计数：会话失效时每次重新 acquire 都应 +1。"""
        prov = H5SessionProvider(_bs())
        with mock.patch.object(prov.bootstrapper, "acquire",
                               return_value=Session(request_id="r")):
            prov.acquire()
            prov.acquire()
        self.assertEqual(prov.refreshes, 2)

    def test_env_requires_site_and_signature(self) -> None:
        full = {H5_ENV_SITE: SITE, H5_ENV_SIGNATURE: SIG, H5_ENV_TOKEN: TOKEN}
        self.assertIsNotNone(h5_bootstrapper_from_env(full))
        for missing in (H5_ENV_SITE, H5_ENV_SIGNATURE):
            partial = {k: v for k, v in full.items() if k != missing}
            with self.subTest(missing=missing):
                self.assertIsNone(h5_bootstrapper_from_env(partial))

    def test_env_requires_some_cookie_or_token(self) -> None:
        env = {H5_ENV_SITE: SITE, H5_ENV_SIGNATURE: SIG}
        self.assertIsNone(h5_bootstrapper_from_env(env))

    def test_env_accepts_full_cookie_string(self) -> None:
        bs = h5_bootstrapper_from_env({
            H5_ENV_SITE: SITE, H5_ENV_SIGNATURE: SIG,
            H5_ENV_COOKIE: "X-API-TOKEN=%s; X-API-UUID=%s" % (TOKEN, UUID),
        })
        assert bs is not None
        self.assertEqual(bs.credentials.token, TOKEN)
        self.assertEqual(bs.credentials.uuid, UUID)

    def test_env_token_falls_back_to_app_token(self) -> None:
        """同一份账号凭据：App 放请求头、H5 放 cookie，应可复用。"""
        bs = h5_bootstrapper_from_env({
            H5_ENV_SITE: SITE, H5_ENV_SIGNATURE: SIG,
            SESSION_ENV_APP_TOKEN: TOKEN, SESSION_ENV_APP_UUID: UUID,
        })
        assert bs is not None
        self.assertEqual(bs.credentials.token, TOKEN)

    def test_env_signature_falls_back_to_app_signature(self) -> None:
        """x-api-xxx 是站点级固定值，H5 与 App 用同一个（实测）。"""
        bs = h5_bootstrapper_from_env({
            H5_ENV_SITE: SITE, SESSION_ENV_APP_SIGNATURE: SIG,
            H5_ENV_TOKEN: TOKEN,
        })
        assert bs is not None
        self.assertEqual(bs.signature, SIG)

    def test_provider_chain_prefers_h5_first(self) -> None:
        """H5 cookie 引导应在链首（与浏览器行为一致，且不需人工登录）。"""
        env = {
            H5_ENV_SITE: SITE, H5_ENV_SIGNATURE: SIG, H5_ENV_TOKEN: TOKEN,
            SESSION_ENV_APP_TOKEN: "t", SESSION_ENV_APP_UUID: "u",
            SESSION_ENV_APP_SIGNATURE: SIG,
            "LEYU_REQUEST_ID": "rid",
        }
        names = [m["provider"]
                 for m in make_session_provider(env=env).describe()["members"]]
        self.assertEqual(names[0], "h5-cookie")
        self.assertIn("app-launch", names)
        self.assertIn("env", names)

    def test_chain_falls_back_when_h5_fails(self) -> None:
        """H5 cookie 失效时应回退到后面的来源，而不是整体失败。"""
        env = {
            H5_ENV_SITE: SITE, H5_ENV_SIGNATURE: SIG, H5_ENV_TOKEN: TOKEN,
            "LEYU_REQUEST_ID": "fallback-rid",
        }
        p = make_session_provider(env=env)
        with mock.patch.object(H5SessionBootstrapper, "acquire",
                               side_effect=SessionError("cookie 已失效")):
            self.assertEqual(p.acquire().request_id, "fallback-rid")

    def test_app_can_be_disabled_also_disables_h5(self) -> None:
        """`include_app=False` 是「只用命令/文件/环境」的总开关。"""
        env = {H5_ENV_SITE: SITE, H5_ENV_SIGNATURE: SIG, H5_ENV_TOKEN: TOKEN}
        p = make_session_provider(env=env, include_app=False)
        self.assertNotIn("h5-cookie", json.dumps(p.describe()))


# --------------------------------------------------------------------------- #
# 真实网络（默认跳过）
# --------------------------------------------------------------------------- #

@unittest.skipUnless(os.environ.get("AI_FOOTBALL_ONLINE") == "1",
                     "在线用例默认跳过（设 AI_FOOTBALL_ONLINE=1 开启）")
class TestOnlineReality(unittest.TestCase):
    """真实环境：H5 cookie → requestId → 业务 API 取到真实数据。"""

    @classmethod
    def setUpClass(cls) -> None:
        boot = h5_bootstrapper_from_env()
        if boot is None:
            raise unittest.SkipTest(
                "需要 LEYU_H5_SITE / LEYU_H5_SIGNATURE / LEYU_H5_TOKEN")
        cls.bs = boot

    def test_live_domains_decrypt(self) -> None:
        self.assertTrue(self.bs.fetch_live_domains())

    def test_acquire_returns_usable_session(self) -> None:
        s = self.bs.acquire()
        self.assertTrue(s.request_id)
        self.assertTrue(s.host.startswith("http"), s.host)
        self.assertTrue(s.origin.startswith("http"), s.origin)

    def test_source_fetches_real_odds(self) -> None:
        from collector.sources import LEYUSource
        src = LEYUSource(session_provider=H5SessionProvider(self.bs))
        snaps, issues = src.fetch(max_matches=5)
        self.assertEqual(issues, [])
        self.assertGreater(len(snaps), 0)
        self.assertEqual({s.source for s in snaps}, {"乐鱼API"})

    def test_live_count_matches_page(self) -> None:
        """展示场次必须与乐鱼一致：进行中数量取自同一份赛程。"""
        from collector.sources import LEYUSource
        src = LEYUSource(session_provider=H5SessionProvider(self.bs))
        counts = src.live_count()
        self.assertGreater(counts["live"], 0)
        self.assertGreaterEqual(counts["live_all_sports"], counts["live"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
