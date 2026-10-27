import asyncio
import unittest

import httpx

from app.worker import LeaseLost, failure_category


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


if __name__ == "__main__":
    unittest.main()
