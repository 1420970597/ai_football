"""Global API failures keep diagnostics useful without copying secret values."""

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.main import _safe_exception_frames, unexpected_error
from starlette.requests import Request


def request_with_id(value: str) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/readyz", "headers": [], "query_string": b""})
    request.state.request_id = value
    return request


class ApiErrorLoggingTests(unittest.TestCase):
    def test_traceback_formatter_contains_only_bounded_code_locations(self):
        try:
            raise RuntimeError("postgresql://user:secret@example.invalid/db?sslmode=require")
        except RuntimeError as exc:
            frames = _safe_exception_frames(exc)
        self.assertRegex(frames, r"test_traceback_formatter_contains_only_bounded_code_locations:\d+")
        self.assertNotIn("postgresql://", frames)
        self.assertNotIn("secret", frames)

    def test_unexpected_handler_does_not_log_exception_text_or_dsn(self):
        request = request_with_id("safe-request-123")
        error = RuntimeError("password=secret-value host=private-db.internal")
        with patch("app.main.log.error") as logged:
            response = asyncio.run(unexpected_error(request, error))
        self.assertEqual(500, response.status_code)
        self.assertEqual({"detail": "服务暂不可用", "request_id": "safe-request-123"}, json.loads(response.body))
        rendered = " ".join(str(value) for call in logged.call_args_list for value in call.args)
        self.assertIn("safe-request-123", rendered)
        self.assertIn("RuntimeError", rendered)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("private-db.internal", rendered)
