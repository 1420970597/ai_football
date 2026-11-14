"""Transient internal reads recover without repeating provider work."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import hermes_client


class ConfigurationReadRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def settings(self):
        return SimpleNamespace(review_config_url='http://internal.invalid/internal/review/config',
                               hermes_api_key='isolated-token', hermes_provider='custom:isolated',
                               hermes_model='isolated-model')

    async def test_one_read_timeout_recovers_with_fresh_validated_configuration(self):
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ReadTimeout('isolated timeout', request=request)
            return httpx.Response(200, json={'model': 'isolated-model', 'provider': 'custom:isolated',
                                           'configuration_version': 'a'*64})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await hermes_client.resolve_review_settings(self.settings(), client=client)
        self.assertEqual(2, calls)
        self.assertEqual('a'*64, result.configuration_version)

    async def test_authentication_denial_does_not_retry_or_use_stale_configuration(self):
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(403)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await hermes_client.resolve_review_settings(self.settings(), client=client)
        self.assertEqual(1, calls)

    async def test_repeated_timeout_stops_after_two_requests(self):
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout('isolated timeout', request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(httpx.ReadTimeout):
                await hermes_client.resolve_review_settings(self.settings(), client=client)
        self.assertEqual(2, calls)
