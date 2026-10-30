"""Migration regression: legacy MFA data is inert and no step-up API remains."""

import unittest
import uuid

from app import auth
import test_identity as legacy
from app.security import token_hash


@unittest.skipUnless(legacy.TEST_URL, "requires explicitly configured temporary test database")
class RemovedSecondaryAuthTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = legacy.IdentityIntegrationTests.asyncTearDown
    asyncSetUp = legacy.IdentityIntegrationTests.asyncSetUp
    admin_login = legacy.IdentityIntegrationTests.admin_login
    register = legacy.IdentityIntegrationTests.register
    csrf = legacy.IdentityIntegrationTests.csrf

    async def test_legacy_totp_secret_does_not_gate_admin_login(self):
        legacy_secret = await self.pool.fetchval(
            "SELECT totp_secret_ciphertext FROM v2_users WHERE id=$1", self.admin_id
        )
        self.assertTrue(legacy_secret)
        response = await self.client.post(
            "/api/v1/admin/login",
            json={"email": "admin@example.invalid", "password": legacy.PASSWORD},
        )
        self.assertEqual(200, response.status_code, response.text)

    async def test_mfa_recovery_and_step_up_routes_are_removed(self):
        await self.admin_login()
        requests = [
            ("GET", "/api/v1/admin/mfa/status"),
            ("POST", "/api/v1/admin/mfa/enrollment"),
            ("POST", "/api/v1/admin/mfa/recovery-codes"),
            ("POST", "/api/v1/admin/recovery-login"),
            ("POST", "/api/v1/admin/reauthenticate"),
            ("POST", "/api/v1/auth/reauthenticate"),
        ]
        for method, path in requests:
            response = await self.client.request(
                method, path, headers=self.csrf(True), json={}
            )
            self.assertEqual(404, response.status_code, path)

    async def test_stale_step_up_timestamp_does_not_block_leyu_binding(self):
        await self.register()
        token = self.client.cookies[auth.USER_COOKIE]
        await self.pool.execute(
            "UPDATE v2_sessions SET reauthenticated_at=now()-interval '30 days' WHERE token_hash=$1",
            token_hash(token),
        )
        response = await self.client.put(
            "/api/v1/accounts/leyu",
            headers=self.csrf(),
            json={"account_label": "隔离测试账户", "username": "fixture-user", "password": "fixture-password"},
        )
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("unverified", response.json()["state"])
        state = await self.client.get("/api/v1/accounts/leyu")
        self.assertEqual("unverified", state.json()["account"]["health_state"])
        self.assertTrue(any(task["kind"] == "verify" for task in state.json()["tasks"]))

    async def test_csrf_and_role_boundaries_remain_after_step_up_removal(self):
        await self.register()
        denied = await self.client.put(
            "/api/v1/accounts/leyu/auto-betting", json={"enabled": False}
        )
        self.assertEqual(403, denied.status_code)
        await self.admin_login()
        denied = await self.client.put(
            "/api/v1/accounts/leyu",
            headers=self.csrf(True),
            json={"account_label": "错误身份", "username": "fixture-user", "password": "fixture-password"},
        )
        self.assertEqual(403, denied.status_code)
