"""只在显式提供临时数据库时运行身份和会员接口行为验收。"""

import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import os
import struct
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import AsyncMock

import asyncpg
from fastapi import FastAPI
import httpx

from app import auth, database
from app.membership import resolve_memberships
from app.routes import accounts, admin, public
from app.security import decrypt_secret, encrypt_secret, hash_password, token_hash, totp_valid, verify_password


TEST_URL = os.environ.get("IDENTITY_TEST_DATABASE_URL", "")
PASSWORD = "test-only-password-1234"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def otp(secret=TOTP_SECRET, now=None):
    key = base64.b32decode(secret)
    digest = hmac.new(key, struct.pack(">Q", int((time.time() if now is None else now) // 30)), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000:06}"


class SecurityTests(unittest.TestCase):
    def test_password_hash_normal_wrong_and_malformed(self):
        stored = hash_password(PASSWORD)
        self.assertTrue(verify_password(PASSWORD, stored))
        self.assertFalse(verify_password("wrong", stored))
        self.assertFalse(verify_password(PASSWORD, ""))
        self.assertFalse(verify_password(PASSWORD, stored.replace("16384", "1073741824")))
        self.assertFalse(verify_password(PASSWORD, stored[:-10]))

    def test_totp_valid_invalid_and_boundary(self):
        self.assertTrue(totp_valid(TOTP_SECRET, otp()))
        self.assertFalse(totp_valid("bad!", "123456"))
        self.assertFalse(totp_valid(TOTP_SECRET, "12345x"))
        self.assertFalse(totp_valid(TOTP_SECRET, "１２３４５６"))
        self.assertFalse(totp_valid("", "123456"))
        self.assertFalse(totp_valid(TOTP_SECRET, "999999", now=0))
        now = 300000000
        self.assertTrue(totp_valid(TOTP_SECRET, otp(now=now - 30), now=now))
        self.assertTrue(totp_valid(TOTP_SECRET, otp(now=now + 30), now=now))
        self.assertFalse(totp_valid(TOTP_SECRET, otp(now=now - 90), now=now))

    def test_secret_encryption_uses_unique_nonce(self):
        key = bytes(range(32))
        encrypted = encrypt_secret(key, "test-session-value")
        self.assertEqual("test-session-value", decrypt_secret(key, encrypted))
        self.assertNotEqual(encrypted, encrypt_secret(key, "test-session-value"))
        with self.assertRaises(Exception):
            decrypt_secret(bytes(32), encrypted)


class MembershipTests(unittest.TestCase):
    def test_overlapping_paid_and_free_merge_true_capabilities(self):
        now = datetime.now(timezone.utc)
        base = {"id": uuid.uuid4(), "starts_at": now - timedelta(days=1), "status": "active", "enabled": True}
        rows = [base | {"code": "free", "name": "免费", "price": Decimal(0), "ends_at": now + timedelta(days=30000),
                        "entitlements": {"expert_summary": True, "expert_full_history": False}},
                base | {"code": "vip", "name": "会员", "price": Decimal(100), "ends_at": now + timedelta(days=30),
                        "entitlements": {"expert_full_history": True}}]
        result = resolve_memberships(rows, now)
        self.assertEqual("vip", result["code"])
        self.assertTrue(result["entitlements"]["expert_summary"])
        self.assertTrue(result["entitlements"]["expert_full_history"])

    def test_future_cancelled_disabled_and_expiry_rejected(self):
        now = datetime.now(timezone.utc)
        base = {"id": uuid.uuid4(), "code": "vip", "name": "会员", "starts_at": now, "ends_at": now + timedelta(days=1)}
        for patch in [{"starts_at": now + timedelta(seconds=1)}, {"ends_at": now}, {"enabled": False}, {"status": "cancelled"}]:
            with self.subTest(patch=patch):
                self.assertIsNone(resolve_memberships([base | patch], now))


@unittest.skipUnless(TEST_URL, "需要显式临时身份数据库连接；不使用生产默认配置")
class IdentityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_name = "identity_" + uuid.uuid4().hex
        self.control = await asyncpg.connect(TEST_URL)
        await self.control.execute(f'CREATE DATABASE "{self.db_name}"')
        url = TEST_URL.rsplit("/", 1)[0] + "/" + self.db_name
        self.pool = await database.open_pool(SimpleNamespace(database_url=url), migrate=True)
        self.app = FastAPI()
        self.app.state.settings = SimpleNamespace(cookie_secure=False, encryption_key=bytes(range(32)), hermes_api_key="test")
        self.app.state.hermes_probe = SimpleNamespace(check=AsyncMock(return_value="unverified"))
        self.app.state.decrypt_secret = lambda value: decrypt_secret(bytes(range(32)), value)
        for router in (auth.router, admin.router, accounts.router, public.router):
            self.app.include_router(router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")
        self.admin_id = uuid.uuid4()
        await self.pool.execute(
            "INSERT INTO v2_users(id,email,display_name,password_hash,role,totp_secret_ciphertext) "
            "VALUES($1,'admin@example.invalid','管理员',$2,'admin',$3)", self.admin_id, hash_password(PASSWORD),
            encrypt_secret(bytes(range(32)), TOTP_SECRET),
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await database.close_pool()
        await self.control.execute(f'DROP DATABASE "{self.db_name}"')
        await self.control.close()

    async def register(self, email="user@example.invalid"):
        response = await self.client.post("/api/v1/auth/register", json={"email": email, "display_name": "用户", "password": PASSWORD})
        self.assertEqual(201, response.status_code, response.text)
        return response.json()

    def csrf(self, admin_session=False):
        return {"X-CSRF-Token": self.client.cookies[auth.ADMIN_CSRF_COOKIE if admin_session else auth.CSRF_COOKIE]}

    async def admin_login(self):
        response = await self.client.post("/api/v1/admin/login", json={"email": "admin@example.invalid", "password": PASSWORD, "totp_code": otp()})
        self.assertEqual(200, response.status_code, response.text)
        return response

    async def paid_plan(self):
        response = await self.client.put("/api/v1/admin/membership/plans/vip", headers=self.csrf(True), json={
            "code": "vip", "name": "专家会员", "price": "100.00", "duration_days": 30,
            "entitlements": {"expert_full_history": True, "automatic_betting": True}, "enabled": True,
        })
        self.assertEqual(200, response.status_code, response.text)

    async def grant(self, key=None, days=30, email="user@example.invalid"):
        body = {"email": email, "plan_code": "vip", "days": days,
                "idempotency_key": str(key or uuid.uuid4()), "reason": "测试人工开通"}
        return await self.client.post("/api/v1/admin/membership/grants", headers=self.csrf(True), json=body)

    async def test_register_login_cookie_hash_logout_and_relogin(self):
        user = await self.register()
        self.assertNotIn("access_token", user)
        token = self.client.cookies[auth.USER_COOKIE]
        stored = await self.pool.fetchval("SELECT token_hash FROM v2_sessions WHERE user_id=$1", uuid.UUID(user["user"]["id"]))
        self.assertEqual(token_hash(token), stored)
        self.assertNotEqual(token, stored)
        self.assertEqual(200, (await self.client.get("/api/v1/auth/me")).status_code)
        self.assertEqual(403, (await self.client.post("/api/v1/auth/logout")).status_code)
        self.assertEqual(200, (await self.client.post("/api/v1/auth/logout", headers=self.csrf())).status_code)
        self.assertEqual(401, (await self.client.get("/api/v1/auth/me")).status_code)
        login = await self.client.post("/api/v1/auth/login", json={"email": "USER@example.invalid", "password": PASSWORD})
        self.assertEqual(200, login.status_code, login.text)
        self.assertNotIn("access_token", login.json())

    async def test_duplicate_register_and_bad_credentials(self):
        await self.register()
        duplicate = await self.client.post("/api/v1/auth/register", json={"email": "USER@example.invalid", "display_name": "用户", "password": PASSWORD})
        self.assertEqual(409, duplicate.status_code)
        for email in ("user@example.invalid", "missing@example.invalid", "admin@example.invalid"):
            response = await self.client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})
            self.assertEqual(401, response.status_code)

    async def test_user_and_admin_sessions_remain_independent(self):
        user = await self.register()
        await self.admin_login()
        result = await self.client.get("/api/v1/auth/me")
        self.assertEqual(user["user"]["id"], result.json()["user"]["id"])
        self.assertEqual("admin", (await self.client.get("/api/v1/admin/me")).json()["user"]["role"])
        # 即使另一个有效管理 Cookie 同时存在，用户操作仍只接受用户 CSRF。
        self.assertEqual(403, (await self.client.patch("/api/v1/auth/profile", headers=self.csrf(True), json={"display_name": "新用户"})).status_code)
        self.assertEqual(200, (await self.client.patch("/api/v1/auth/profile", headers=self.csrf(), json={"display_name": "新用户"})).status_code)
        self.assertEqual(200, (await self.client.post("/api/v1/admin/logout", headers=self.csrf(True))).status_code)
        self.assertEqual(200, (await self.client.get("/api/v1/auth/me")).status_code)
        self.assertEqual(401, (await self.client.get("/api/v1/admin/me")).status_code)

    async def test_user_logout_preserves_admin_and_replayed_totp_rejected(self):
        await self.register()
        code = otp()
        response = await self.client.post("/api/v1/admin/login", json={"email": "admin@example.invalid", "password": PASSWORD, "totp_code": code})
        self.assertEqual(200, response.status_code)
        replay = await self.client.post("/api/v1/admin/login", json={"email": "admin@example.invalid", "password": PASSWORD, "totp_code": code})
        self.assertEqual(401, replay.status_code)
        self.assertEqual(200, (await self.client.post("/api/v1/auth/logout", headers=self.csrf())).status_code)
        self.assertEqual(200, (await self.client.get("/api/v1/admin/me")).status_code)

    async def test_registration_login_and_admin_csrf_boundaries(self):
        await self.register()
        self.assertEqual(401, (await self.client.get("/api/v1/admin/status")).status_code)
        await self.admin_login()
        self.assertEqual(403, (await self.client.put("/api/v1/admin/settings", headers=self.csrf(), json={"values": {}})).status_code)
        self.assertEqual(200, (await self.client.put("/api/v1/admin/settings", headers=self.csrf(True), json={"values": {}})).status_code)

    async def test_secure_cookie_attributes(self):
        self.app.state.settings.cookie_secure = True
        self.client.cookies.clear()
        response = await self.client.post("/api/v1/auth/register", json={
            "email": "secure@example.invalid", "display_name": "安全用户", "password": PASSWORD,
        })
        self.assertEqual(201, response.status_code, response.text)
        cookies = "\n".join(response.headers.get_list("set-cookie")).lower()
        self.assertIn("v2_session=", cookies)
        self.assertIn("secure", cookies)
        self.assertIn("httponly", cookies)
        self.assertIn("samesite=strict", cookies)
        csrf_cookie = next(item for item in response.headers.get_list("set-cookie") if item.startswith(auth.CSRF_COOKIE + "="))
        self.assertNotIn("httponly", csrf_cookie.lower())


    async def test_admin_settings_audit_diff_and_hot_update_state_are_truthful(self):
        await self.admin_login()
        response = await self.client.put(
            "/api/v1/admin/settings", headers=self.csrf(True),
            json={"expected_leyu_version": 1, "values": {
                "leyu_api_base": "https://business.example.invalid",
                "leyu_request_id": "private-session-for-test",
            }},
        )
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual("persisted_unverified", body["application"]["state"])
        self.assertFalse(body["application"]["runtime_acknowledged"])
        self.assertEqual(2, body["leyu_version"])
        details = await self.pool.fetchval(
            "SELECT details FROM v2_audit_log WHERE action='settings_updated' ORDER BY created_at DESC LIMIT 1"
        )
        self.assertEqual(2, details["configuration_version"])
        self.assertEqual("persisted_unverified", details["application_state"])
        self.assertFalse(details["runtime_acknowledged"])
        self.assertEqual("https://business.example.invalid", details["diff"]["leyu_api_base"]["after"]["value"])
        self.assertIsNone(details["diff"]["leyu_request_id"]["after"]["value"])
        self.assertNotIn("private-session-for-test", str(details))

        status = await self.client.get("/api/v1/admin/status")
        self.assertEqual(200, status.status_code, status.text)
        state = status.json()["leyu_configuration"]
        self.assertEqual(2, state["revision"])
        self.assertEqual("persisted_unverified", state["application_state"])
        self.assertFalse(state["runtime_acknowledged"])

    async def test_client_role_fields_cannot_promote_account(self):
        response = await self.client.post("/api/v1/auth/register", json={"email": "forged@example.invalid", "display_name": "用户", "password": PASSWORD, "role": "admin"})
        self.assertEqual(201, response.status_code)
        self.assertEqual("user", response.json()["user"]["role"])
        self.assertEqual(401, (await self.client.get("/api/v1/admin/users")).status_code)
        login = await self.client.post("/api/v1/admin/login", json={"email": "forged@example.invalid", "password": PASSWORD, "totp_code": otp()})
        self.assertEqual(401, login.status_code)

    async def test_audit_failure_rolls_back_sensitive_setting(self):
        await self.admin_login()
        await self.pool.execute("CREATE FUNCTION fail_test_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test audit failure'; END $$")
        await self.pool.execute("CREATE TRIGGER fail_test_audit BEFORE INSERT ON v2_audit_log FOR EACH ROW EXECUTE FUNCTION fail_test_audit()")
        with self.assertRaises(asyncpg.RaiseError):
            await self.client.put("/api/v1/admin/settings", headers=self.csrf(True), json={"values": {"auto_betting_enabled": True}})
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_admin_settings"))

    async def test_login_rate_limit_and_disabled_user(self):
        data = await self.register()
        await self.pool.execute("UPDATE v2_users SET disabled_at=now() WHERE id=$1", uuid.UUID(data["user"]["id"]))
        for _ in range(10):
            response = await self.client.post("/api/v1/auth/login", json={"email": "user@example.invalid", "password": PASSWORD})
            self.assertEqual(401, response.status_code)
        response = await self.client.post("/api/v1/auth/login", json={"email": "user@example.invalid", "password": PASSWORD})
        self.assertEqual(429, response.status_code)
        self.assertGreater(int(response.headers["retry-after"]), 0)
        self.assertEqual(401, (await self.client.get("/api/v1/auth/me")).status_code)

    async def test_refresh_rotates_session_and_password_change_revokes_all(self):
        await self.register()
        old = self.client.cookies[auth.USER_COOKIE]
        response = await self.client.post("/api/v1/auth/refresh", headers=self.csrf())
        self.assertEqual(200, response.status_code)
        self.assertNotEqual(old, self.client.cookies[auth.USER_COOKIE])
        self.assertEqual(401, (await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old}"})).status_code)
        response = await self.client.put("/api/v1/auth/password", headers=self.csrf(), json={"current_password": PASSWORD, "new_password": "replacement-test-password-1234"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(401, (await self.client.get("/api/v1/auth/me")).status_code)
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_sessions WHERE session_kind='user'"))

    async def test_expired_session_cannot_refresh(self):
        await self.register()
        await self.pool.execute("UPDATE v2_sessions SET expires_at=now()-interval '1 second'")
        response = await self.client.post("/api/v1/auth/refresh", headers=self.csrf())
        self.assertEqual(401, response.status_code)

    async def test_paid_membership_wins_display_and_disabled_future_rejected(self):
        user = await self.register()
        await self.admin_login()
        await self.paid_plan()
        grant = await self.grant()
        self.assertEqual(201, grant.status_code, grant.text)
        member = (await self.client.get("/api/v1/auth/me")).json()["membership"]
        self.assertEqual("vip", member["code"])
        self.assertTrue(member["entitlements"]["expert_full_history"])
        self.assertTrue(member["entitlements"]["expert_summary"])
        self.assertFalse((await self.client.get("/api/v1/public/signals")).json()["limited"])
        await self.pool.execute("UPDATE v2_memberships SET starts_at=now()+interval '1 day' WHERE id=$1", uuid.UUID(grant.json()["id"]))
        member = (await self.client.get("/api/v1/auth/me")).json()["membership"]
        self.assertEqual("free", member["code"])
        self.assertTrue((await self.client.get("/api/v1/public/signals")).json()["limited"])
        await self.pool.execute("UPDATE v2_memberships SET starts_at=now()-interval '1 day' WHERE id=$1", uuid.UUID(grant.json()["id"]))
        await self.pool.execute("UPDATE v2_membership_plans SET enabled=FALSE WHERE code='vip'")
        self.assertEqual("free", (await self.client.get("/api/v1/auth/me")).json()["membership"]["code"])
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_users WHERE id=$1", uuid.UUID(user["user"]["id"])))

    async def test_bootstrap_does_not_replace_existing_admin(self):
        previous = await self.pool.fetchrow("SELECT password_hash,totp_secret_ciphertext FROM v2_users WHERE id=$1", self.admin_id)
        settings = SimpleNamespace(bootstrap_admin_email="different@example.invalid", bootstrap_admin_password=PASSWORD,
                                   bootstrap_admin_totp_secret=TOTP_SECRET, encryption_key=bytes(range(32)))
        await auth.bootstrap_admin(self.pool, settings)
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_users WHERE role='admin'"))
        after = await self.pool.fetchrow("SELECT password_hash,totp_secret_ciphertext FROM v2_users WHERE id=$1", self.admin_id)
        self.assertEqual(previous, after)

    async def test_manual_grant_idempotency_conflict_and_extension(self):
        await self.register()
        await self.admin_login()
        await self.paid_plan()
        key = uuid.uuid4()
        first = await self.grant(key)
        repeated = await self.grant(key)
        self.assertEqual(201, first.status_code, first.text)
        self.assertEqual(first.json()["id"], repeated.json()["id"])
        self.assertTrue(repeated.json()["replayed"])
        self.assertEqual(409, (await self.grant(key, days=31)).status_code)
        extension = await self.grant(days=7)
        self.assertEqual(201, extension.status_code, extension.text)
        self.assertEqual(first.json()["ends_at"], extension.json()["starts_at"])
        count = await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='membership_granted'")
        self.assertEqual(2, count)

    async def test_revoke_membership_removes_capability_and_stops_betting(self):
        user = await self.register()
        await self.admin_login()
        await self.paid_plan()
        grant = await self.grant()
        await self.pool.execute("INSERT INTO v2_user_bet_settings(user_id,auto_enabled,emergency_stopped) VALUES($1,TRUE,FALSE)", uuid.UUID(user["user"]["id"]))
        endpoint = "/api/v1/admin/membership/grants/" + grant.json()["id"] + "/revoke"
        for _ in range(2):
            response = await self.client.post(endpoint, headers=self.csrf(True), json={"reason": "测试撤销会员"})
            self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("free", (await self.client.get("/api/v1/auth/me")).json()["membership"]["code"])
        self.assertEqual(False, await self.pool.fetchval("SELECT auto_enabled FROM v2_user_bet_settings"))
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='membership_revoked'"))

    async def test_personal_account_rebind_stops_old_permission_and_stays_isolated(self):
        data = await self.register()
        user_id = uuid.UUID(data["user"]["id"])
        await self.pool.execute("INSERT INTO v2_user_bet_settings(user_id,auto_enabled,emergency_stopped) VALUES($1,TRUE,FALSE)", user_id)
        response = await self.client.put("/api/v1/accounts/leyu", headers=self.csrf(), json={"account_label": "本人测试", "username": "test-only-account", "password": "not-real"})
        self.assertEqual(200, response.status_code)
        account = await self.client.get("/api/v1/accounts/leyu")
        self.assertFalse(account.json()["betting"]["auto_enabled"])
        self.assertNotIn("password", account.text)
        self.assertNotIn("encrypted_credentials", account.text)
        self.client.cookies.clear()
        await self.register("another@example.invalid")
        self.assertIsNone((await self.client.get("/api/v1/accounts/leyu")).json()["account"])


if __name__ == "__main__":
    unittest.main()
