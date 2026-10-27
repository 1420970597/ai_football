import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from services.collector.leyu_protocol import AuthExpired, LeyuError, RateLimited
from services.collector.leyu_runtime import LeyuRuntime


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []

        def handle(request):
            self.requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"version": 7, "items": {"leyu_api_base": "https://current.invalid", "leyu_request_id": "current"}})
            if request.url.path.endswith("/claim"):
                return httpx.Response(201, json={"attempt_id": "attempt", "version": 7})
            if request.method == "PUT":
                return httpx.Response(200, json={"version": 8})
            return httpx.Response(200, json={"status": "success"})

        self.api = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.runtime = LeyuRuntime(self.api, "https://api.invalid/internal", {"leyu_api_base": "https://obsolete.invalid", "leyu_user_agent": "test-agent"})

    async def asyncTearDown(self):
        await self.api.aclose()

    async def test_remote_hot_configuration_wins_and_callbacks_use_revision(self):
        await self.runtime.load()
        self.assertEqual("https://current.invalid", self.runtime.config["leyu_api_base"])
        self.assertEqual("test-agent", self.runtime.config["leyu_user_agent"])
        self.assertEqual("attempt", await self.runtime._claim("a" * 64))
        await self.runtime._persist({"leyu_request_id": "renewed"})
        await self.runtime._finish("attempt", "success", {"login_submitted": False})
        self.assertEqual(8, self.runtime.version)
        body = json.loads(self.requests[2].content)
        self.assertEqual(("attempt", 7), (body["attempt_id"], body["expected_version"]))
        self.assertEqual("renewed", self.runtime.config["leyu_request_id"])

    async def test_revoked_remote_credential_cannot_return_from_local_defaults(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"version": 9, "items": {}}))) as api:
            runtime = LeyuRuntime(api, "https://api.invalid/internal", {"leyu_request_id": "revoked-old-session",
                "leyu_app_token": "revoked-app-token", "leyu_api_base": "https://obsolete.invalid", "leyu_user_agent": "configured-agent"})
            config = await runtime.load()
        self.assertEqual({"leyu_user_agent": "configured-agent"}, config)

    async def test_invalid_candidate_keeps_previous_config_revision_and_validation_time(self):
        previous = {"leyu_api_base": "https://working.invalid", "leyu_request_id": "working-session"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "version": 8, "items": {"leyu_api_base": "file:///etc/passwd", "leyu_request_id": "replacement"},
        }))) as api:
            runtime = LeyuRuntime(api, "https://api.invalid/internal")
            runtime.config = previous.copy()
            runtime.version = 6
            runtime.config_validated_at = "2026-10-20T00:00:00+00:00"
            with self.assertRaises(LeyuError) as raised:
                await runtime.load()
        self.assertEqual("invalid_base_url", raised.exception.category)
        self.assertEqual(previous, runtime.config)
        self.assertEqual(6, runtime.version)
        self.assertEqual("2026-10-20T00:00:00+00:00", runtime.config_validated_at)
        self.assertEqual("invalid_base_url", runtime.config_validation_error)

    async def test_collect_uses_last_valid_session_when_hot_configuration_is_invalid(self):
        previous = {"leyu_api_base": "https://working.invalid", "leyu_request_id": "working-session"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "version": 8, "items": {"leyu_api_base": "file:///etc/passwd", "leyu_request_id": "replacement"},
        }))) as api:
            runtime = LeyuRuntime(api, "https://api.invalid/internal")
            runtime.config = previous.copy()
            runtime.version = 6
            client = SimpleNamespace(fetch=AsyncMock(return_value=[]), last_report={"fixtures": 0})
            with patch("services.collector.leyu_runtime.LeyuClient.from_config", return_value=client):
                self.assertEqual([], await runtime.collect())
        client.fetch.assert_awaited_once()
        self.assertEqual(previous, runtime.config)
        self.assertEqual(6, runtime.version)
        self.assertEqual("invalid_base_url", runtime.config_validation_error)
        self.assertEqual(6, runtime.last_report["config_version"])

    async def test_partial_odds_refresh_retains_last_projection_without_claiming_completeness(self):
        previous_quote = {"market_id": "4", "period": "full_time", "line": "-0.75", "selection": "home",
                          "price": "2.11", "source_updated_at": "2026-10-27T12:00:00+00:00"}
        self.runtime.fixtures = {"leyu:m1": {"id": "leyu:m1", "source_match_id": "m1", "phase": "scheduled",
            "markets": [previous_quote], "markets_complete": True}}
        updated = {"id": "leyu:m1", "source_id": "leyu:m1", "source_match_id": "m1", "phase": "scheduled",
                   "markets": [], "markets_complete": False}
        client = SimpleNamespace(fetch=AsyncMock(return_value=[updated]), last_report={"odds_requested_fixtures": 0})
        with patch("services.collector.leyu_runtime.LeyuClient.from_config", return_value=client):
            result = await self.runtime.collect(odds_mids=[])
        client.fetch.assert_awaited_once_with(odds_mids=[])
        self.assertEqual([previous_quote], result[0]["markets"])
        self.assertFalse(result[0]["markets_complete"])
        self.assertEqual([previous_quote], self.runtime.fixtures["leyu:m1"]["markets"])

    async def test_invalid_collection_budget_is_rejected_before_revision_acknowledgement(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "version": 8, "items": {"leyu_batch_interval_seconds": "nan"},
        }))) as api:
            runtime = LeyuRuntime(api, "https://api.invalid/internal")
            with self.assertRaises(LeyuError) as raised:
                await runtime.load()
        self.assertEqual("invalid_collection_budget", raised.exception.category)
        self.assertEqual(0, runtime.version)
        self.assertEqual("invalid_collection_budget", runtime.config_validation_error)

    async def test_invalid_proxy_settings_do_not_replace_last_valid_session(self):
        previous = {"leyu_api_base": "https://working.invalid", "leyu_request_id": "working-session"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "version": 8, "items": {"leyu_api_base": "https://replacement.invalid", "leyu_request_id": "replacement",
                                    "leyu_login_proxy_url": "proxy.invalid:not-a-port"},
        }))) as api:
            runtime = LeyuRuntime(api, "https://api.invalid/internal")
            runtime.config = previous.copy()
            runtime.version = 6
            with self.assertRaises(LeyuError) as raised:
                await runtime.load()
        self.assertEqual("invalid_login_proxy", raised.exception.category)
        self.assertEqual(previous, runtime.config)
        self.assertEqual(6, runtime.version)

    async def test_explicit_expiry_renews_once_then_retries_collection(self):
        failed = SimpleNamespace(fetch=AsyncMock(side_effect=AuthExpired("session_expired")))
        healthy = SimpleNamespace(fetch=AsyncMock(return_value=[]), last_report={"fixtures": 0})

        async def renew(reason):
            self.assertEqual("session_expired", reason)
            self.runtime.config["leyu_request_id"] = "renewed"

        with patch("services.collector.leyu_runtime.LeyuClient.from_config", side_effect=[failed, healthy]), \
             patch("services.collector.leyu_runtime.AppSessionRenewer") as factory:
            factory.return_value.renew = AsyncMock(side_effect=renew)
            self.assertEqual([], await self.runtime.collect())
            self.assertEqual(1, factory.return_value.renew.await_count)
            self.assertEqual(7, self.runtime.last_report["config_version"])

    async def test_app_identity_can_bootstrap_before_business_base_exists(self):
        document = {"version": 1, "items": {"leyu_app_host": "https://app.invalid", "leyu_app_uuid": "authorized-device",
                                                   "leyu_app_login_name": "authorized-account",
                                                   "leyu_app_login_password": "isolated-test-password"}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=document))) as api:
            runtime = LeyuRuntime(api, "https://api.invalid/internal")
            healthy = SimpleNamespace(fetch=AsyncMock(return_value=[]), last_report={"fixtures": 0})

            async def renew(reason):
                self.assertEqual("authorized_bootstrap", reason)
                runtime.config.update(leyu_api_base="https://business.invalid", leyu_request_id="new-business-session")

            with patch("services.collector.leyu_runtime.LeyuClient.from_config", return_value=healthy) as factory, \
                 patch("services.collector.leyu_runtime.AppSessionRenewer") as renewal:
                renewal.return_value.renew = AsyncMock(side_effect=renew)
                self.assertEqual([], await runtime.collect())
                self.assertEqual(1, factory.call_count)
                renewal.return_value.renew.assert_awaited_once_with("authorized_bootstrap")

    async def test_rate_limit_permission_and_network_error_never_login(self):
        for error in (RateLimited("upstream_rate_limited"), LeyuError("http_access_denied", "403"), LeyuError("upstream_timeout")):
            client = SimpleNamespace(fetch=AsyncMock(side_effect=error))
            with patch("services.collector.leyu_runtime.LeyuClient.from_config", return_value=client), \
                 patch("services.collector.leyu_runtime.AppSessionRenewer") as factory:
                with self.assertRaises(LeyuError):
                    await self.runtime.collect()
                factory.assert_not_called()

    async def test_rpc_failure_redacts_body_and_url_secrets(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(403, text="private-secret"))) as api:
            runtime = LeyuRuntime(api, "https://api.invalid/internal")
            with self.assertRaises(LeyuError) as raised:
                await runtime.load()
            self.assertNotIn("private-secret", str(raised.exception))
            self.assertNotIn("api.invalid", str(raised.exception))
            self.assertEqual("renewal_api_unauthorized", raised.exception.category)

    async def test_renewal_rpc_statuses_have_actionable_categories(self):
        expected = {
            409: "renewal_state_conflict",
            422: "renewal_request_invalid",
            503: "renewal_api_unavailable",
        }
        for status, category in expected.items():
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request, status=status: httpx.Response(status, text="private-response")
            )) as api:
                runtime = LeyuRuntime(api, "https://api.invalid/internal")
                with self.assertRaises(LeyuError) as raised:
                    await runtime.load()
                self.assertEqual(category, raised.exception.category)
                self.assertNotIn("private-response", str(raised.exception))

    def test_incremental_quotes_preserve_suspension_and_reject_old_unknown_time(self):
        quote = {"market_id": "4", "period": "full_time", "line": "-.5", "selection": "home", "price": "2.0",
                 "status": "suspended", "source_updated_at": "2026-01-01T12:00:00+00:00"}
        self.runtime.fixtures = {"leyu:1": {"id": "leyu:1", "phase": "live", "markets": [quote], "markets_complete": True}}
        base = {"fixture_id": "leyu:1", "received_at": "2026-01-01T12:00:01+00:00"}
        changed = {**quote, "price": "2.2", "status": "active", "source_updated_at": "2026-01-01T12:00:01+00:00", "status_evidence": False}
        result, resync = self.runtime.apply_push([{**base, "markets": [changed]}])
        self.assertFalse(resync)
        self.assertFalse(result[0]["markets_complete"])
        self.assertEqual("suspended", result[0]["markets"][0]["status"])
        self.assertEqual("2.2", result[0]["markets"][0]["price"])
        old = {**changed, "source_updated_at": "2026-01-01T11:59:00+00:00"}
        result, _ = self.runtime.apply_push([{**base, "markets": [old]}])
        self.assertEqual([], result)
        unknown = {**changed, "source_updated_at": None}
        result, resync = self.runtime.apply_push([{**base, "markets": [unknown]}])
        self.assertTrue(resync)
        self.assertEqual([], result)

    def test_unknown_fixture_requires_resync(self):
        self.assertEqual(([], True), self.runtime.apply_push([{"fixture_id": "leyu:missing"}]))


if __name__ == "__main__":
    unittest.main()
