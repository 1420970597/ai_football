import asyncio
import unittest
from unittest.mock import patch

import httpx

from app.hermes_client import HermesProviderUnavailable
from app.worker import LeaseLost, failure_category, retry_delay_seconds


class ReviewFailureClassificationTests(unittest.TestCase):
    def test_transient_failures_are_retryable_without_exposing_details(self):
        self.assertEqual(("timeout", True), failure_category(asyncio.TimeoutError()))
        self.assertEqual(("network", True), failure_category(httpx.ConnectError("secret endpoint")))
        self.assertEqual(("provider_transient", True), failure_category(
            httpx.HTTPStatusError("provider body", request=httpx.Request("POST", "https://model.invalid"),
                                  response=httpx.Response(429)),
        ))

    def test_permanent_and_lease_failures_are_bounded(self):
        self.assertEqual(("lease_lost", True), failure_category(LeaseLost("private lease details")))
        self.assertEqual(("provider_auth", False), failure_category(
            httpx.HTTPStatusError("secret response", request=httpx.Request("POST", "https://model.invalid"),
                                  response=httpx.Response(401)),
        ))
        self.assertEqual(("visual_input_invalid", False), failure_category(
            RuntimeError("remote image has unsupported Telegram image type"),
        ))

    def test_provider_error_uses_transient_category_and_longer_backoff(self):
        self.assertEqual(("provider_transient", True), failure_category(HermesProviderUnavailable()))
        with patch('app.worker.random.randint', return_value=0):
            self.assertEqual(60, retry_delay_seconds('provider_transient', 1))
            self.assertEqual(120, retry_delay_seconds('provider_transient', 2))
            self.assertEqual(300, retry_delay_seconds('provider_transient', 8))


if __name__ == "__main__":
    unittest.main()
