import os
import unittest
from unittest.mock import patch

from app.config import database_url, load_settings


class ConfigurationTests(unittest.TestCase):
    def test_worker_does_not_require_user_or_collector_secrets(self):
        environment = {"DATABASE_URL": "postgresql://test@isolated/test", "HERMES_API_KEY": "test-worker-only"}
        with patch.dict(os.environ, environment, clear=True):
            settings = load_settings(role="worker")
        self.assertEqual(b"", settings.encryption_key)
        self.assertEqual("", settings.collector_token)
        self.assertEqual("test-worker-only", settings.hermes_api_key)

    def test_api_does_not_receive_or_need_model_provider_key(self):
        environment = {
            "DATABASE_URL": "postgresql://test@isolated/test",
            "APP_SESSION_SECRET": "01" * 32,
            "DATA_ENCRYPTION_KEY": "02" * 32,
            "EXPERT_MCP_TOKEN": "test-mcp", "COLLECTOR_INTERNAL_TOKEN": "test-collector",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = load_settings()
        self.assertEqual("", settings.hermes_api_key)

    def test_missing_or_invalid_key_fails_without_echoing_its_value(self):
        secret = "sensitive-wrong-length"
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test@isolated/test", "APP_SESSION_SECRET": secret}, clear=True):
            with self.assertRaises(RuntimeError) as failure:
                load_settings()
        self.assertNotIn(secret, str(failure.exception))

    def test_execution_is_disabled_by_default_and_worker_has_no_execution_permission(self):
        environment = {
            "DATABASE_URL": "postgresql://test@isolated/test", "APP_SESSION_SECRET": "01" * 32,
            "DATA_ENCRYPTION_KEY": "02" * 32, "EXPERT_MCP_TOKEN": "test-mcp",
            "COLLECTOR_INTERNAL_TOKEN": "test-collector", "EXECUTOR_INTERNAL_TOKEN": "test-executor",
            "HERMES_API_KEY": "test-worker-only",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertFalse(load_settings().live_orders_enabled)
            self.assertEqual("test-executor", load_settings().executor_token)
            with patch.dict(os.environ, {"LIVE_ORDERS_ENABLED": "true"}):
                self.assertTrue(load_settings().live_orders_enabled)
                worker = load_settings(role="worker")
                self.assertFalse(worker.live_orders_enabled)
                self.assertEqual("", worker.executor_token)

    def test_legacy_quoted_credentials_are_encoded_in_dsn(self):
        environment = {"PG_BASE_URL": '"remote-db"', "PG_BASE_PORT": "5432", "PG_USER": "'name@host'", "PG_PASSWORD": "'p$@ss:/word'", "PG_DATASET": '"foot ball"'}
        with patch.dict(os.environ, environment, clear=True):
            url = database_url()
        self.assertIn("name%40host:p%24%40ss%3A%2Fword@remote-db:5432/foot%20ball", url)
        self.assertIn("sslmode=require", url)


if __name__ == "__main__":
    unittest.main()
