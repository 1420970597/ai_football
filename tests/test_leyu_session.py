import base64
import errno
import json
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlsplit

import httpx

from services.collector.leyu_protocol import LeyuError
from services.collector.leyu_session import (
    AppSessionRenewer, AppSigner, DomainResolver, LoginProxy, ProxyLease,
    LOGIN_PATH, LAUNCH_PATH, PRE_INFO_PATH, aes_cbc,
)


def config(**changes):
    return {"leyu_app_host": "https://app.invalid", "leyu_app_uuid": "device",
            "leyu_app_login_name": "sample-account", "leyu_app_login_password": "sample-password",
            "leyu_app_login_signature": "site-signature", "leyu_app_signature": "game-signature",
            "leyu_app_token": "existing-token", **changes}


def launch():
    return {"status_code": 6000, "data": {"url": "https://business.invalid/index.html?token=real-session",
                                               "h5Url": "https://ui.invalid/index.html?client=app"}}


class SignerTests(unittest.TestCase):
    def test_native_headers_bind_provided_exit_with_private_keys(self):
        routes = {name: {"key": "k" * 16, "iv": "i" * 16} for name in ("", "/site/api", "/game/api")}
        headers = AppSigner(routes).headers("/site/api", "8.8.8.8")
        bound = aes_cbc(base64.b64decode(headers["x-api-hack-xxxxx"]), b"k" * 16, b"i" * 16, decrypt=True)
        self.assertEqual("8.8.8.8", json.loads(bound)["xx"])
        self.assertTrue(bytes.fromhex(headers["x-api-xxx"]))

    def test_invalid_keys_and_ip_rejected(self):
        with self.assertRaises(LeyuError):
            AppSigner({})
        signer = AppSigner({name: {"key": "k" * 16, "iv": "i" * 16} for name in ("", "/site/api", "/game/api")})
        with self.assertRaises(LeyuError):
            signer.headers("/site/api", "not-an-ip")


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_configuration_is_validated_before_extraction(self):
        proxy = LoginProxy({"leyu_login_proxy_extract_url": "https://provider.invalid/get",
                            "leyu_login_proxy_authkey": "only-one-half"})
        with self.assertRaisesRegex(LeyuError, "proxy_credentials_incomplete"):
            proxy.validate_configuration()
        proxy = LoginProxy({"leyu_login_proxy_url": "proxy.invalid:not-a-port"})
        with self.assertRaisesRegex(LeyuError, "invalid_login_proxy"):
            proxy.validate_configuration()
        for template in ("{password", "{password!r}", "{unknown}"):
            proxy = LoginProxy({"leyu_login_proxy_password_template": template})
            with self.assertRaisesRegex(LeyuError, "invalid_proxy_password_template"):
                proxy.validate_configuration()

    async def test_direct_proxy_gets_short_fresh_sticky_session(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as direct:
            proxy = LoginProxy({"leyu_login_proxy_url": "http://sample:password@geo.iproyal.com:12321"})
            one = await proxy.acquire(direct)
            two = await proxy.acquire(direct)
        self.assertNotEqual(one.url, two.url)
        self.assertIn("_session-", one.url)
        self.assertIn("_lifetime-59s_killswitch-1", one.url)
        self.assertLessEqual(one.remaining(), 55)

    async def test_extraction_honors_expiration_and_supplied_ip(self):
        payload = {"code": "SUCCESS", "data": [{"server": "8.8.8.8:1234", "proxy_ip": "1.1.1.1"}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as direct:
            lease = await LoginProxy({"leyu_login_proxy_extract_url": "https://provider.invalid/get?key=redacted",
                                      "leyu_login_proxy_authkey": "qg-auth-key",
                                      "leyu_login_proxy_authpwd": "qg-auth-password"}).acquire(direct)
        self.assertEqual("1.1.1.1", lease.egress_ip)
        self.assertEqual("http://qg-auth-key:qg-auth-password@8.8.8.8:1234", lease.url)
        with self.assertRaises(LeyuError):
            ProxyLease("http://unused", time.monotonic() - 1).remaining()

    async def test_extraction_fails_before_consuming_ip_when_proxy_auth_is_missing(self):
        calls = []
        transport = httpx.MockTransport(lambda request: calls.append(request.url.path) or httpx.Response(200))
        async with httpx.AsyncClient(transport=transport) as direct:
            with self.assertRaisesRegex(LeyuError, "proxy_credentials_missing"):
                await LoginProxy({"leyu_login_proxy_extract_url": "https://provider.invalid/get"}).acquire(direct)
        self.assertEqual([], calls)

    async def test_extraction_api_key_is_a_complete_auth_method_without_proxy_credentials(self):
        payload = {"code": "SUCCESS", "data": [{"server": "8.8.8.8:1234", "proxy_ip": "1.1.1.1"}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as direct:
            lease = await LoginProxy({
                "leyu_login_proxy_extract_url": "https://provider.invalid/get?key=api-key-only&num=1",
            }).acquire(direct)
        self.assertEqual("http://8.8.8.8:1234", lease.url)
        self.assertEqual("1.1.1.1", lease.egress_ip)

    async def test_invalid_extraction_does_not_leak_provider_body(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": "secret-value"}))) as direct:
            with self.assertRaises(LeyuError) as raised:
                await LoginProxy({"leyu_login_proxy_extract_url": "https://provider.invalid/get",
                                  "leyu_login_proxy_authkey": "qg-auth-key",
                                  "leyu_login_proxy_authpwd": "qg-auth-password"}).acquire(direct)
        self.assertNotIn("secret-value", str(raised.exception))


class RenewalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.updates = []
        self.direct_paths = []
        self.proxy_paths = []
        self.proxy_urls = []

    async def claim(self, account_hash):
        self.events.append(("claim", account_hash))
        return "attempt1"

    async def finish(self, attempt, status, metadata):
        self.events.append(("finish", status, metadata))

    async def persist(self, updates):
        self.updates.append(updates)

    def renewer(self, cfg, handler, proxy_handler=None):
        def direct(request):
            self.direct_paths.append(request.url.path)
            return handler(request)
        def proxy(url):
            self.proxy_urls.append(url)
            self.assertTrue(url.startswith(("http://", "https://")))
            def response(request):
                self.proxy_paths.append(request.url.path)
                return proxy_handler(request)
            return httpx.MockTransport(response)
        return AppSessionRenewer(cfg, claim=self.claim, finish=self.finish, persist=self.persist,
                                 direct_transport=httpx.MockTransport(direct),
                                 login_transport_factory=proxy if proxy_handler else None,
                                 verify_public_hosts=False)

    async def test_valid_app_token_launches_without_password_login_or_proxy(self):
        def direct(request):
            if request.url.path == PRE_INFO_PATH:
                self.assertEqual("", request.headers["x-api-token"])
                return httpx.Response(200, json={"status_code": 6000, "data": {"ip": "1.1.1.1"}})
            self.assertEqual(LAUNCH_PATH, request.url.path)
            body = json.loads(request.content)
            self.assertEqual("false", body["sendMoney"])
            return httpx.Response(200, json=launch())
        updates = await self.renewer(config(leyu_login_proxy_url="http://sample:password@proxy.invalid:1234"), direct).renew()
        self.assertEqual("real-session", updates["leyu_request_id"])
        self.assertEqual("https://ui.invalid", updates["leyu_origin"])
        self.assertNotIn(LOGIN_PATH, self.direct_paths)
        self.assertEqual([], self.proxy_paths)
        self.assertEqual("success", self.events[-1][1])
        self.assertFalse(self.events[-1][2]["login_submitted"])

    async def test_expired_app_token_only_login_uses_proxy_then_launch_direct(self):
        def direct(request):
            if request.url.path == PRE_INFO_PATH:
                return httpx.Response(200, json={"status_code": 6000, "data": {"ip": "1.1.1.1"}})
            self.assertEqual(LAUNCH_PATH, request.url.path)
            if request.headers["x-api-token"] == "existing-token":
                return httpx.Response(200, json={"status_code": 6001})
            self.assertEqual("new-token", request.headers["x-api-token"])
            return httpx.Response(200, json=launch())
        def proxy(request):
            self.assertEqual(LOGIN_PATH, request.url.path)
            self.assertEqual("application/json; charset=utf-8", request.headers["content-type"])
            self.assertEqual("gzip", request.headers["accept-encoding"])
            self.assertEqual(request.content, json.dumps(json.loads(request.content), separators=(",", ":")).encode())
            body = json.loads(request.content)
            self.assertEqual(32, len(body["password"]))
            self.assertNotIn("sample-password", str(body))
            return httpx.Response(200, json={"status_code": 6000, "data": {"token": "new-token"}})
        cfg = config(leyu_login_proxy_url="http://sample:password@proxy.invalid:1234")
        await self.renewer(cfg, direct, proxy).renew()
        self.assertEqual([PRE_INFO_PATH, LAUNCH_PATH, LAUNCH_PATH], self.direct_paths)
        self.assertEqual([LOGIN_PATH], self.proxy_paths)
        self.assertEqual("new-token", self.updates[0]["leyu_app_token"])
        self.assertEqual("success", self.events[-1][1])

    async def test_login_uses_v1_path_and_dynamic_proxy_egress_without_probe(self):
        self.assertEqual("/site/api/v1/user/login", LOGIN_PATH)
        routes = {name: {"key": "k" * 16, "iv": "i" * 16} for name in ("", "/site/api", "/game/api")}
        with tempfile.TemporaryDirectory() as tmp:
            signing_path = Path(tmp) / "signing.json"
            signing_path.write_text(json.dumps({"routes": routes}), encoding="utf-8")
            direct_calls = []
            def direct(request):
                direct_calls.append(request.url.path)
                if request.url.host == "provider.invalid":
                    return httpx.Response(200, json={"code": "SUCCESS", "data": [
                        {"server": "8.8.8.8:1234", "proxy_ip": "9.9.9.9"}]})
                if request.url.path == PRE_INFO_PATH:
                    return httpx.Response(200, json={"status_code": 6000, "data": {"ip": "1.1.1.1"}})
                if request.url.path == LAUNCH_PATH:
                    return httpx.Response(200, json=launch())
                self.fail("unexpected direct App request")
            def proxy(request):
                self.assertEqual(LOGIN_PATH, request.url.path)
                bound = aes_cbc(base64.b64decode(request.headers["x-api-hack-xxxxx"]),
                                b"k" * 16, b"i" * 16, decrypt=True)
                self.assertEqual("9.9.9.9", json.loads(bound)["xx"])
                return httpx.Response(200, json={"status_code": 6000, "data": {"token": "new-token"}})
            cfg = config(leyu_app_token="", leyu_app_signing_config=str(signing_path),
                         leyu_login_proxy_extract_url="https://provider.invalid/get",
                         leyu_login_proxy_authkey="qg-auth-key", leyu_login_proxy_authpwd="qg-auth-password")
            updates = await self.renewer(cfg, direct, proxy).renew()
        self.assertEqual("real-session", updates["leyu_request_id"])
        self.assertEqual([PRE_INFO_PATH, "/get", LAUNCH_PATH], direct_calls)
        self.assertEqual([LOGIN_PATH], self.proxy_paths)
        self.assertEqual("qg-auth-key", urlsplit(self.proxy_urls[0]).username)
        self.assertEqual("qg-auth-password", urlsplit(self.proxy_urls[0]).password)

    async def test_proxy_http_504_is_recorded_as_login_proxy_response(self):
        def direct(request):
            if request.url.path == PRE_INFO_PATH:
                return httpx.Response(200, json={"status_code": 6000, "data": {"ip": "1.1.1.1"}})
            return httpx.Response(200, json={"status_code": 6001})
        def proxy(request):
            return httpx.Response(504, text="private gateway response")
        with self.assertRaises(LeyuError) as raised:
            await self.renewer(config(leyu_login_proxy_url="http://sample:password@proxy.invalid:1234"),
                               direct, proxy).renew()
        self.assertEqual("proxy_login_http_rejected", raised.exception.category)
        self.assertEqual("504", raised.exception.code)
        self.assertNotIn("private gateway response", str(raised.exception))
        self.assertEqual("unknown", self.events[-1][1])
        self.assertEqual("proxy_login_http_rejected", self.events[-1][2]["category"])

    async def test_proxy_connect_and_read_failures_are_separated_without_exception_text(self):
        def direct(request):
            if request.url.path == PRE_INFO_PATH:
                return httpx.Response(200, json={"status_code": 6000, "data": {"ip": "1.1.1.1"}})
            return httpx.Response(200, json={"status_code": 6001})

        for error_type, expected in ((httpx.ConnectError, "proxy_login_connect_failure"),
                                     (httpx.ReadTimeout, "proxy_login_read_timeout")):
            self.events.clear()
            def proxy(request, error_type=error_type):
                raise error_type("secret-proxy-and-target-details", request=request)
            with self.assertRaises(LeyuError) as raised:
                await self.renewer(config(leyu_login_proxy_url="http://sample:password@proxy.invalid:1234"),
                                   direct, proxy).renew()
            self.assertEqual(expected, raised.exception.category)
            self.assertNotIn("secret-proxy-and-target-details", str(raised.exception))
            self.assertEqual(expected, self.events[-1][2]["category"])

    async def test_proxy_connection_refusal_is_classified_without_os_error_text(self):
        def direct(request):
            if request.url.path == PRE_INFO_PATH:
                return httpx.Response(200, json={"status_code": 6000, "data": {"ip": "1.1.1.1"}})
            return httpx.Response(200, json={"status_code": 6001})
        def proxy(request):
            error = httpx.ConnectError("sensitive endpoint details", request=request)
            raise error from ConnectionRefusedError(errno.ECONNREFUSED, "secret proxy host")
        with self.assertRaises(LeyuError) as raised:
            await self.renewer(config(leyu_login_proxy_url="http://sample:password@proxy.invalid:1234"),
                               direct, proxy).renew()
        self.assertEqual("proxy_login_connection_refused", raised.exception.category)
        self.assertNotIn("secret proxy host", str(raised.exception))
        self.assertEqual("proxy_login_connection_refused", self.events[-1][2]["category"])

    async def test_6031_is_rejected_once_not_automatically_replayed(self):
        def direct(request):
            if request.url.path == PRE_INFO_PATH:
                return httpx.Response(200, json={"status_code": 6000, "data": {"ip": "1.1.1.1"}})
            return httpx.Response(200, json={"status_code": 6001})
        def proxy(request):
            return httpx.Response(200, text="8.8.8.8") if request.method == "GET" else httpx.Response(200, json={"status_code": 6031, "message": "private response"})
        cfg = config(leyu_login_proxy_url="http://sample:password@proxy.invalid:1234")
        with self.assertRaises(LeyuError) as raised:
            await self.renewer(cfg, direct, proxy).renew()
        self.assertEqual("6031", raised.exception.code)
        self.assertNotIn("private response", str(raised.exception))
        self.assertEqual("rejected", self.events[-1][1])
        self.assertEqual(1, self.proxy_paths.count(LOGIN_PATH))

    async def test_missing_claim_stops_all_requests(self):
        async def reject(account): return ""
        renewer = AppSessionRenewer(config(), claim=reject, finish=self.finish, persist=self.persist)
        with self.assertRaisesRegex(LeyuError, "claim_rejected"):
            await renewer.renew()
        self.assertEqual([], self.updates)

    async def test_network_error_does_not_request_password_login(self):
        def direct(request):
            return httpx.Response(503)
        with self.assertRaises(LeyuError):
            await self.renewer(config(), direct).renew()
        self.assertNotIn(LOGIN_PATH, self.direct_paths)
        self.assertEqual("failed", self.events[-1][1])

    async def test_permission_refusal_not_misread_as_app_expiration(self):
        def direct(request):
            if request.url.path == PRE_INFO_PATH:
                return httpx.Response(200, json={"status_code": 6000, "data": {"ip": "1.1.1.1"}})
            return httpx.Response(200, json={"status_code": 6003})
        with self.assertRaisesRegex(LeyuError, "venue_launch_rejected"):
            await self.renewer(config(), direct).renew()
        self.assertNotIn(LOGIN_PATH, self.direct_paths)

    async def test_unknown_login_result_is_persisted_and_new_token_survives_failed_launch(self):
        calls = 0
        def direct(request):
            nonlocal calls
            if request.url.path == PRE_INFO_PATH:
                return httpx.Response(200, json={"status_code": 6000, "data": {"ip": "1.1.1.1"}})
            if request.url.path == LOGIN_PATH:
                return httpx.Response(200, json={"status_code": 6000, "data": {"token": "new-token"}})
            calls += 1
            return httpx.Response(200, json={"status_code": 6001 if calls == 1 else 6415})
        with self.assertRaises(LeyuError):
            await self.renewer(config(), direct).renew()
        self.assertEqual("new-token", self.updates[0]["leyu_app_token"])
        self.assertEqual("unknown", self.events[-1][1])

    async def test_other_failure_reason_cannot_start_renewal(self):
        with self.assertRaisesRegex(LeyuError, "reason_not_authorized"):
            await self.renewer(config(), lambda request: httpx.Response(500)).renew("rate_limited")
        self.assertEqual([], self.events)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_encrypted_domain_feed_and_cache(self):
        payload = json.dumps({"domain_group": [{"domain": "https://new.invalid"}]}).encode()
        encrypted = base64.b64encode(aes_cbc(payload, b"k" * 16, b"i" * 16))
        calls = 0
        def response(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, content=encrypted)
        resolver = DomainResolver({"feeds": ["https://feed.invalid/config"], "key": "k" * 16, "iv": "i" * 16}, verify_public=False)
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as direct:
            self.assertEqual(["https://new.invalid"], await resolver.candidates(direct))
            self.assertEqual(["https://new.invalid"], await resolver.candidates(direct))
        self.assertEqual(1, calls)

    async def test_feed_follows_only_configured_https_redirect_hosts(self):
        payload = json.dumps({"domain_group": [{"domain": "https://new.invalid"}]}).encode()
        encrypted = base64.b64encode(aes_cbc(payload, b"k" * 16, b"i" * 16))
        seen = []
        def response(request):
            seen.append(request.url.host)
            if request.url.host == "feed.invalid":
                return httpx.Response(302, headers={"location": "https://cdn.invalid/config"})
            return httpx.Response(200, content=encrypted)
        resolver = DomainResolver({"feeds": ["https://feed.invalid/config"], "redirect_hosts": ["cdn.invalid"],
                                   "key": "k" * 16, "iv": "i" * 16}, verify_public=False)
        async with httpx.AsyncClient(transport=httpx.MockTransport(response), follow_redirects=False) as direct:
            self.assertEqual(["https://new.invalid"], await resolver.candidates(direct))
        self.assertEqual(["feed.invalid", "cdn.invalid"], seen)

    async def test_feed_rejects_unconfigured_redirect_host(self):
        def response(request):
            return httpx.Response(302, headers={"location": "https://untrusted.invalid/config"})
        resolver = DomainResolver({"feeds": ["https://feed.invalid/config"], "seed_hosts": []}, verify_public=False)
        async with httpx.AsyncClient(transport=httpx.MockTransport(response), follow_redirects=False) as direct:
            with self.assertRaisesRegex(LeyuError, "domain_discovery_unavailable"):
                await resolver.candidates(direct)

    async def test_invalid_discovery_no_credentials_forwarded(self):
        def response(request):
            self.assertNotIn("x-api-token", request.headers)
            return httpx.Response(200, text="bad document")
        resolver = DomainResolver({"feeds": ["https://feed.invalid/config"]}, verify_public=False)
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as direct:
            with self.assertRaises(LeyuError):
                await resolver.candidates(direct)


if __name__ == "__main__":
    unittest.main()
