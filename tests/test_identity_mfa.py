"""管理员验证器换机的隔离 PostgreSQL 行为验收；没有真实身份或外部请求。"""
import asyncio
import unittest
import uuid
from unittest.mock import patch

import httpx

from app import admin_mfa, auth, identity_lifecycle
from app.security import encrypt_secret, new_token, token_hash, totp_valid
import test_identity as legacy


@unittest.skipUnless(legacy.TEST_URL, "需要显式临时数据库连接")
class AdminMfaTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = legacy.IdentityIntegrationTests.asyncTearDown
    admin_login = legacy.IdentityIntegrationTests.admin_login
    register = legacy.IdentityIntegrationTests.register
    csrf = legacy.IdentityIntegrationTests.csrf

    async def asyncSetUp(self):
        await legacy.IdentityIntegrationTests.asyncSetUp(self)
        self.app.include_router(identity_lifecycle.router)
        self.app.include_router(admin_mfa.router)
        await self.admin_login()
        self.original_cipher = await self.cipher()

    async def cipher(self):
        return await self.pool.fetchval("SELECT totp_secret_ciphertext FROM v2_users WHERE id=$1", self.admin_id)

    async def start(self, replace=False):
        return await self.client.post("/api/v1/admin/mfa/enrollment", headers=self.csrf(True), json={"replace_existing": replace})

    async def enrollment(self):
        response = await self.start()
        self.assertEqual(201, response.status_code, response.text)
        return response.json()

    async def confirm(self, pending, code=None):
        return await self.client.post(f"/api/v1/admin/mfa/enrollment/{pending['enrollment_id']}/confirm", headers=self.csrf(True), json={"totp_code": code or legacy.otp(pending["secret"])})

    async def recovery_codes(self):
        response = await self.client.post("/api/v1/admin/mfa/recovery-codes", headers=self.csrf(True))
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["codes"]

    async def extra_session(self):
        token = new_token()
        await self.pool.execute("INSERT INTO v2_sessions(token_hash,csrf_hash,user_id,session_kind,expires_at,reauthenticated_at) VALUES($1,$2,$3,'admin',now()+interval '1 hour',now())", token_hash(token), token_hash(new_token()), self.admin_id)
        return token

    async def test_begin_encrypts_pending_and_status_does_not_redisclose_secret(self):
        pending = await self.enrollment()
        self.assertEqual(self.original_cipher, await self.cipher())
        row = await self.pool.fetchrow("SELECT * FROM v2_admin_mfa_enrollments")
        self.assertNotEqual(pending["secret"], row["encrypted_secret"])
        self.assertEqual(pending["secret"], self.app.state.decrypt_secret(row["encrypted_secret"]))
        self.assertIn("otpauth://totp/", pending["otpauth_uri"])
        status = await self.client.get("/api/v1/admin/mfa/status")
        self.assertEqual(200, status.status_code, status.text)
        self.assertTrue(status.json()["configured"])
        self.assertEqual(pending["enrollment_id"], status.json()["pending"]["enrollment_id"])
        self.assertNotIn(pending["secret"], status.text)
        self.assertNotIn(pending["secret"], str(await self.pool.fetch("SELECT details FROM v2_audit_log")))

    async def test_wrong_new_code_keeps_old_factor_and_backup_codes(self):
        old_codes = await self.recovery_codes()
        pending = await self.enrollment()
        bad = next(f"{n:06d}" for n in range(10) if not totp_valid(pending["secret"], f"{n:06d}"))
        response = await self.confirm(pending, bad)
        self.assertEqual(401, response.status_code, response.text)
        self.assertEqual(self.original_cipher, await self.cipher())
        self.assertEqual(10, await self.pool.fetchval("SELECT count(*) FROM v2_admin_recovery_codes WHERE consumed_at IS NULL AND revoked_at IS NULL"))
        self.assertTrue(await self.pool.fetchval("SELECT code_hash=$1 FROM v2_admin_recovery_codes WHERE code_hash=$1", token_hash(old_codes[0])))

    async def test_confirm_rotates_factor_backup_codes_and_other_admin_sessions(self):
        await self.register()
        user_token = self.client.cookies[auth.USER_COOKIE]
        current_token = self.client.cookies[auth.ADMIN_COOKIE]
        other_token = await self.extra_session()
        old_codes = await self.recovery_codes()
        pending = await self.enrollment()
        response = await self.confirm(pending)
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertFalse(result["already_confirmed"])
        self.assertEqual(10, len(result["recovery_codes"]))
        self.assertEqual(pending["secret"], self.app.state.decrypt_secret(await self.cipher()))
        self.assertEqual("", await self.pool.fetchval("SELECT encrypted_secret FROM v2_admin_mfa_enrollments WHERE id=$1", uuid.UUID(pending["enrollment_id"])))
        self.assertIsNone(await self.pool.fetchval("SELECT token_hash FROM v2_sessions WHERE token_hash=$1", token_hash(other_token)))
        self.assertIsNotNone(await self.pool.fetchval("SELECT token_hash FROM v2_sessions WHERE token_hash=$1", token_hash(current_token)))
        self.assertIsNotNone(await self.pool.fetchval("SELECT token_hash FROM v2_sessions WHERE token_hash=$1", token_hash(user_token)))
        self.assertEqual(200, (await self.client.get("/api/v1/admin/me")).status_code)
        self.assertEqual(200, (await self.client.get("/api/v1/auth/me")).status_code)
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_admin_recovery_codes WHERE code_hash=$1 AND revoked_at IS NULL", token_hash(old_codes[0])))
        stored = await self.pool.fetch("SELECT code_hash FROM v2_admin_recovery_codes WHERE revoked_at IS NULL")
        self.assertEqual({token_hash(code) for code in result["recovery_codes"]}, {row["code_hash"] for row in stored})
        audit = str(await self.pool.fetch("SELECT details FROM v2_audit_log"))
        for code in result["recovery_codes"] + [pending["secret"]]:
            self.assertNotIn(code, audit)

    async def test_repeated_and_concurrent_confirmation_never_redisclose_codes(self):
        pending = await self.enrollment()
        responses = await asyncio.gather(self.confirm(pending), self.confirm(pending))
        self.assertEqual([200, 200], [r.status_code for r in responses])
        self.assertEqual([False, True], sorted(r.json()["already_confirmed"] for r in responses))
        self.assertEqual(1, sum(r.json()["recovery_codes"] is not None for r in responses))
        self.assertEqual(10, await self.pool.fetchval("SELECT count(*) FROM v2_admin_recovery_codes WHERE revoked_at IS NULL"))
        self.assertIsNone((await self.confirm(pending)).json()["recovery_codes"])

    async def test_pending_is_bound_to_initiating_session(self):
        pending = await self.enrollment()
        other = await self.extra_session()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver", headers={"Authorization": "Bearer " + other}) as second:
            response = await second.post(f"/api/v1/admin/mfa/enrollment/{pending['enrollment_id']}/confirm", json={"totp_code": legacy.otp(pending["secret"])})
            self.assertEqual(404, response.status_code, response.text)
            self.assertIsNone((await second.get("/api/v1/admin/mfa/status")).json()["pending"])
            denied = await second.delete(f"/api/v1/admin/mfa/enrollment/{pending['enrollment_id']}")
            self.assertEqual(404, denied.status_code)
        self.assertEqual(self.original_cipher, await self.cipher())

    async def test_csrf_role_and_recent_verification_required(self):
        denied = await self.client.post("/api/v1/admin/mfa/enrollment", json={})
        self.assertEqual(403, denied.status_code)
        await self.pool.execute("UPDATE v2_sessions SET reauthenticated_at=now()-interval '6 minutes' WHERE token_hash=$1", token_hash(self.client.cookies[auth.ADMIN_COOKIE]))
        denied = await self.start()
        self.assertEqual(403, denied.status_code)
        self.assertEqual("reauthentication_required", denied.json()["detail"]["code"])
        self.assertEqual(200, (await self.client.get("/api/v1/admin/mfa/status")).status_code)
        await self.register()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver", headers={"Authorization": "Bearer " + self.client.cookies[auth.USER_COOKIE]}) as user:
            self.assertEqual(403, (await user.get("/api/v1/admin/mfa/status")).status_code)

    async def test_expired_stepup_blocks_confirm_but_allows_cancel(self):
        pending = await self.enrollment()
        await self.pool.execute("UPDATE v2_sessions SET reauthenticated_at=now()-interval '6 minutes' WHERE token_hash=$1", token_hash(self.client.cookies[auth.ADMIN_COOKIE]))
        self.assertEqual(403, (await self.confirm(pending)).status_code)
        response = await self.client.delete(f"/api/v1/admin/mfa/enrollment/{pending['enrollment_id']}", headers=self.csrf(True))
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(self.original_cipher, await self.cipher())
        self.assertEqual("", await self.pool.fetchval("SELECT encrypted_secret FROM v2_admin_mfa_enrollments"))

    async def test_expired_pending_never_replaces_old_factor(self):
        pending = await self.enrollment()
        await self.pool.execute("UPDATE v2_admin_mfa_enrollments SET expires_at=now()-interval '1 second'")
        self.assertEqual(409, (await self.confirm(pending)).status_code)
        self.assertEqual(self.original_cipher, await self.cipher())
        self.assertIsNone((await self.client.get("/api/v1/admin/mfa/status")).json()["pending"])
        newer = await self.enrollment()
        self.assertNotEqual(pending["enrollment_id"], newer["enrollment_id"])

    async def test_replace_requires_explicit_intent_and_revokes_previous_pending(self):
        old = await self.enrollment()
        self.assertEqual(409, (await self.start()).status_code)
        replacement = await self.start(True)
        self.assertEqual(201, replacement.status_code, replacement.text)
        self.assertEqual(409, (await self.confirm(old)).status_code)
        self.assertEqual(self.original_cipher, await self.cipher())
        self.assertEqual(200, (await self.confirm(replacement.json())).status_code)

    async def test_factor_changed_elsewhere_rejects_pending_confirmation(self):
        pending = await self.enrollment()
        other = encrypt_secret(bytes(range(32)), "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        await self.pool.execute("UPDATE v2_users SET totp_secret_ciphertext=$2 WHERE id=$1", self.admin_id, other)
        self.assertEqual(409, (await self.confirm(pending)).status_code)
        self.assertEqual(other, await self.cipher())

    async def test_corrupt_pending_keeps_old_factor(self):
        pending = await self.enrollment()
        await self.pool.execute("UPDATE v2_admin_mfa_enrollments SET encrypted_secret='corrupted'")
        self.assertEqual(503, (await self.confirm(pending)).status_code)
        self.assertEqual(self.original_cipher, await self.cipher())

    async def test_confirmation_audit_failure_rolls_back_entire_rotation(self):
        old_codes = await self.recovery_codes()
        other = await self.extra_session()
        pending = await self.enrollment()
        await self.pool.execute("ALTER TABLE v2_audit_log ADD CONSTRAINT test_block_rotation CHECK(action<>'admin_mfa_changed')")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url="http://testserver", cookies=self.client.cookies) as client:
            response = await client.post(f"/api/v1/admin/mfa/enrollment/{pending['enrollment_id']}/confirm", headers=self.csrf(True), json={"totp_code": legacy.otp(pending["secret"])})
        self.assertEqual(500, response.status_code)
        self.assertEqual(self.original_cipher, await self.cipher())
        self.assertIsNone(await self.pool.fetchval("SELECT consumed_at FROM v2_admin_mfa_enrollments"))
        self.assertIsNotNone(await self.pool.fetchval("SELECT token_hash FROM v2_sessions WHERE token_hash=$1", token_hash(other)))
        self.assertEqual(10, await self.pool.fetchval("SELECT count(*) FROM v2_admin_recovery_codes WHERE revoked_at IS NULL"))
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_admin_recovery_codes WHERE code_hash=$1 AND revoked_at IS NULL", token_hash(old_codes[0])))

    async def test_backup_login_can_reenroll_when_old_device_is_lost(self):
        old_codes = await self.recovery_codes()
        recovered = await self.client.post("/api/v1/admin/recovery-login", json={"email": "admin@example.invalid", "password": legacy.PASSWORD, "recovery_code": old_codes[0]})
        self.assertEqual(200, recovered.status_code, recovered.text)
        pending = await self.enrollment()
        confirmed = await self.confirm(pending)
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        code = confirmed.json()["recovery_codes"][0]
        replay = await self.client.post("/api/v1/admin/recovery-login", json={"email": "admin@example.invalid", "password": legacy.PASSWORD, "recovery_code": old_codes[0]})
        self.assertEqual(401, replay.status_code)
        valid = await self.client.post("/api/v1/admin/recovery-login", json={"email": "admin@example.invalid", "password": legacy.PASSWORD, "recovery_code": code})
        self.assertEqual(200, valid.status_code, valid.text)
        self.assertEqual(401, (await self.client.post("/api/v1/admin/recovery-login", json={"email": "admin@example.invalid", "password": legacy.PASSWORD, "recovery_code": code})).status_code)

    async def test_confirmation_code_is_consumed_for_next_login(self):
        pending = await self.enrollment()
        used = legacy.otp(pending["secret"])
        self.assertEqual(200, (await self.confirm(pending, used)).status_code)
        replay = await self.client.post("/api/v1/admin/login", json={"email": "admin@example.invalid", "password": legacy.PASSWORD, "totp_code": used})
        self.assertEqual(401, replay.status_code)
        step = await self.pool.fetchval("SELECT totp_last_step FROM v2_users WHERE id=$1", self.admin_id)
        next_login = await self.client.post("/api/v1/admin/login", json={"email": "admin@example.invalid", "password": legacy.PASSWORD, "totp_code": legacy.otp(pending["secret"], now=(step + 1) * 30)})
        self.assertEqual(200, next_login.status_code, next_login.text)

    async def test_confirmation_attempts_are_limited_without_altering_old_factor(self):
        pending = await self.enrollment()
        bad = next(f"{n:06d}" for n in range(10) if not totp_valid(pending["secret"], f"{n:06d}"))
        results = [await self.confirm(pending, bad) for _ in range(6)]
        self.assertEqual([401] * 5 + [429], [r.status_code for r in results])
        self.assertIn("retry-after", results[-1].headers)
        self.assertEqual(self.original_cipher, await self.cipher())

    async def test_login_reading_old_factor_cannot_finish_after_rotation(self):
        pending = await self.enrollment()
        old_step = await self.pool.fetchval("SELECT totp_last_step FROM v2_users WHERE id=$1", self.admin_id)
        entered, release = asyncio.Event(), asyncio.Event()
        original = auth._hash_job

        async def paused_hash(*args):
            result = await original(*args)
            entered.set()
            await release.wait()
            return result

        with patch.object(auth, "_hash_job", paused_hash):
            request = asyncio.create_task(self.client.post("/api/v1/admin/login", json={"email": "admin@example.invalid", "password": legacy.PASSWORD, "totp_code": legacy.otp(now=(old_step + 1) * 30)}))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                self.assertEqual(200, (await self.confirm(pending)).status_code)
            finally:
                release.set()
            response = await asyncio.wait_for(request, 5)
        self.assertEqual(401, response.status_code, response.text)
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_sessions WHERE user_id=$1 AND session_kind='admin'", self.admin_id))

    async def test_reauthentication_reading_old_factor_cannot_finish_after_rotation(self):
        pending = await self.enrollment()
        old_step = await self.pool.fetchval("SELECT totp_last_step FROM v2_users WHERE id=$1", self.admin_id)
        entered, release = asyncio.Event(), asyncio.Event()
        original = identity_lifecycle.finish_reauthentication

        async def paused_finish(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        with patch.object(identity_lifecycle, "finish_reauthentication", paused_finish):
            request = asyncio.create_task(self.client.post("/api/v1/admin/reauthenticate", headers=self.csrf(True), json={"password": legacy.PASSWORD, "totp_code": legacy.otp(now=(old_step + 1) * 30)}))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                self.assertEqual(200, (await self.confirm(pending)).status_code)
            finally:
                release.set()
            response = await asyncio.wait_for(request, 5)
        self.assertEqual(401, response.status_code, response.text)
        self.assertIn("验证器已经变更", response.text)

    async def assert_revoked_after_precheck(self, method, path, body=None):
        entered, release = asyncio.Event(), asyncio.Event()
        original = auth.get_identity

        async def paused_identity(request, *args, **kwargs):
            identity = await original(request, *args, **kwargs)
            if request.method == method and request.url.path == path:
                entered.set()
                await release.wait()
            return identity

        with patch.object(auth, "get_identity", paused_identity):
            pending = asyncio.create_task(self.client.request(method, path, headers=self.csrf(True), json=body))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                await self.pool.execute("DELETE FROM v2_sessions WHERE token_hash=$1", token_hash(self.client.cookies[auth.ADMIN_COOKIE]))
            finally:
                release.set()
            response = await asyncio.wait_for(pending, 5)
        self.assertEqual(401, response.status_code, response.text)
        self.assertEqual(self.original_cipher, await self.cipher())
        return response

    async def test_revoked_session_cannot_start_after_precheck(self):
        await self.assert_revoked_after_precheck("POST", "/api/v1/admin/mfa/enrollment", {})
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_admin_mfa_enrollments"))

    async def test_revoked_session_cannot_confirm_after_precheck(self):
        pending = await self.enrollment()
        await self.assert_revoked_after_precheck("POST", f"/api/v1/admin/mfa/enrollment/{pending['enrollment_id']}/confirm", {"totp_code": legacy.otp(pending["secret"])})
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_admin_recovery_codes"))

    async def test_revoked_session_cannot_cancel_after_precheck(self):
        pending = await self.enrollment()
        await self.assert_revoked_after_precheck("DELETE", f"/api/v1/admin/mfa/enrollment/{pending['enrollment_id']}")
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='admin_mfa_enrollment_cancelled'"))

    async def test_revoked_session_cannot_generate_new_backup_codes_after_precheck(self):
        codes = await self.recovery_codes()
        await self.assert_revoked_after_precheck("POST", "/api/v1/admin/mfa/recovery-codes")
        hashes = {row["code_hash"] for row in await self.pool.fetch("SELECT code_hash FROM v2_admin_recovery_codes WHERE revoked_at IS NULL")}
        self.assertEqual({token_hash(code) for code in codes}, hashes)

    async def test_other_session_cannot_replace_codes_after_mfa_rotates(self):
        pending = await self.enrollment()
        other = await self.extra_session()
        entered, release = asyncio.Event(), asyncio.Event()
        original = auth.require_admin

        async def paused_admin(request):
            identity = await original(request)
            if request.url.path.endswith("/recovery-codes"):
                entered.set()
                await release.wait()
            return identity

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver", headers={"Authorization": "Bearer " + other}) as second:
            with patch.object(auth, "require_admin", paused_admin):
                request = asyncio.create_task(second.post("/api/v1/admin/mfa/recovery-codes"))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    rotated = await self.confirm(pending)
                    self.assertEqual(200, rotated.status_code, rotated.text)
                finally:
                    release.set()
                response = await asyncio.wait_for(request, 5)
        self.assertEqual(401, response.status_code, response.text)
        hashes = {row["code_hash"] for row in await self.pool.fetch("SELECT code_hash FROM v2_admin_recovery_codes WHERE revoked_at IS NULL")}
        self.assertEqual({token_hash(code) for code in rotated.json()["recovery_codes"]}, hashes)

    async def test_expired_verification_cannot_generate_codes_after_precheck(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = auth.require_admin

        async def paused_admin(request):
            identity = await original(request)
            entered.set()
            await release.wait()
            return identity

        with patch.object(auth, "require_admin", paused_admin):
            pending = asyncio.create_task(self.client.post("/api/v1/admin/mfa/recovery-codes", headers=self.csrf(True)))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                await self.pool.execute("UPDATE v2_sessions SET reauthenticated_at=now()-interval '6 minutes' WHERE token_hash=$1", token_hash(self.client.cookies[auth.ADMIN_COOKIE]))
            finally:
                release.set()
            response = await asyncio.wait_for(pending, 5)
        self.assertEqual(403, response.status_code, response.text)
        self.assertEqual("reauthentication_required", response.json()["detail"]["code"])
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_admin_recovery_codes"))

    async def test_revoked_admin_cannot_issue_user_password_recovery(self):
        user = await self.register()
        await self.assert_revoked_after_precheck("POST", f"/api/v1/admin/users/{user['user']['id']}/password-recovery", {"reason": "隔离身份已核验", "idempotency_key": str(uuid.uuid4())})
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_password_recoveries"))
