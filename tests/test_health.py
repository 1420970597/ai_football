import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.health import HermesProbe, MCPProbe, StorageProbe


class HermesHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_uses_health_and_caches_concurrent_checks_without_provider_key(self):
        client = AsyncMock()
        client.get.return_value = httpx.Response(200)
        probe = HermesProbe("http://hermes:8642/v1")
        with patch("app.health.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            self.assertEqual(["reachable"] * 4, await asyncio.gather(*(probe.check() for _ in range(4))))
        client.get.assert_awaited_once_with("http://hermes:8642/health")
        factory.assert_called_once_with(timeout=1.5, trust_env=False, follow_redirects=False)

    async def test_timeout_or_http_failure_is_not_reported_as_healthy(self):
        for response in [httpx.Response(503), httpx.ReadTimeout("isolated timeout")]:
            client = AsyncMock()
            if isinstance(response, Exception):
                client.get.side_effect = response
            else:
                client.get.return_value = response
            with patch("app.health.httpx.AsyncClient") as factory:
                factory.return_value.__aenter__.return_value = client
                self.assertEqual("unavailable", await HermesProbe("http://hermes:8642/v1").check())


class StorageAndMCPHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_storage_probe_caches_and_reports_disposable_probe_result(self):
        probe = StorageProbe(ttl=60)
        with patch.object(probe, "_check_sync", return_value="healthy") as check:
            self.assertEqual("healthy", await probe.check())
            self.assertEqual("healthy", await probe.check())
            check.assert_called_once_with()

    async def test_storage_probe_without_configuration_is_explicit(self):
        with patch.dict("os.environ", {key: "" for key in ("S3_ENDPOINT_URL", "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY")}, clear=False):
            self.assertEqual("unconfigured", await StorageProbe().check())

    async def test_mcp_probe_accepts_protocol_method_rejection_as_reachable(self):
        client = AsyncMock()
        client.get.return_value = httpx.Response(405)
        probe = MCPProbe("http://hermes-mcp:8090/mcp", "token")
        with patch("app.health.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            self.assertEqual("reachable", await probe.check())
        client.get.assert_awaited_once()
