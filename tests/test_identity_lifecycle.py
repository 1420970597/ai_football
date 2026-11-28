"""Temporary PostgreSQL only: recovery, step-up, export and safe anonymization behavior."""
import asyncio
from datetime import datetime
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
import unittest
import uuid

import httpx

from app import auth, identity_lifecycle
from app.security import token_hash
import test_identity as legacy


@unittest.skipUnless(legacy.TEST_URL, "requires explicitly configured temporary test database")
class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = legacy.IdentityIntegrationTests.asyncTearDown
    register = legacy.IdentityIntegrationTests.register
    admin_login = legacy.IdentityIntegrationTests.admin_login
    csrf = legacy.IdentityIntegrationTests.csrf

    async def asyncSetUp(self):
        await legacy.IdentityIntegrationTests.asyncSetUp(self)
        self.app.include_router(identity_lifecycle.router)

    async def expire_stepup(self, kind="user"):
        cookie = auth.ADMIN_COOKIE if kind == "admin" else auth.USER_COOKIE
        await self.pool.execute("UPDATE v2_sessions SET reauthenticated_at=now()-interval '6 minutes' WHERE token_hash=$1", token_hash(self.client.cookies[cookie]))

    async def issue(self, user_id, key=None, reason="已核验本人身份，申请恢复"):
        return await self.client.post(f"/api/v1/admin/users/{user_id}/password-recovery", headers=self.csrf(True),
                                      json={"reason": reason, "idempotency_key": str(key or uuid.uuid4())})

    async def reset(self, code, email="user@example.invalid", password="replacement-password-123"):
        return await self.client.post("/api/v1/auth/recovery/complete", json={"email": email, "code": code, "new_password": password})

    async def close(self):
        return await self.client.post("/api/v1/auth/closure", headers=self.csrf(), json={"confirmation_phrase": "注销我的球讯账户"})

    async def test_no_delivery_channel_never_claims_message_sent(self):
        state = await self.client.get("/api/v1/auth/recovery/status")
        self.assertFalse(state.json()["delivery_configured"])
        request = await self.client.post("/api/v1/auth/recovery/request")
        self.assertEqual(503, request.status_code)
        self.assertIn("没有发送", request.text)

    async def test_user_step_up_endpoint_removed_and_export_uses_active_session(self):
        await self.register()
        await self.expire_stepup()
        self.assertEqual(200, (await self.client.get("/api/v1/auth/export")).status_code)
        self.assertEqual(404, (await self.client.post("/api/v1/auth/reauthenticate", json={})).status_code)

    async def test_refresh_rotates_session_without_step_up_state(self):
        await self.register()
        await self.expire_stepup()
        refreshed = await self.client.post("/api/v1/auth/refresh", headers=self.csrf())
        self.assertEqual(200, refreshed.status_code)
        self.assertEqual(200, (await self.client.get("/api/v1/auth/export")).status_code)

    async def test_stale_admin_step_up_timestamp_does_not_block_admin_write(self):
        user = await self.register()
        await self.admin_login()
        await self.expire_stepup("admin")
        denied = await self.client.post("/api/v1/admin/reauthenticate", headers=self.csrf(True), json={})
        self.assertEqual(404, denied.status_code)
        self.assertEqual(201, (await self.issue(user["user"]["id"])).status_code)

    async def test_expired_admin_can_still_logout(self):
        await self.admin_login()
        await self.expire_stepup("admin")
        self.assertEqual(200, (await self.client.post("/api/v1/admin/logout", headers=self.csrf(True))).status_code)

    async def test_manual_recovery_is_hashed_single_use_and_revokes_sessions(self):
        user = await self.register()
        await self.admin_login()
        issued = await self.issue(user["user"]["id"])
        self.assertEqual(201, issued.status_code, issued.text)
        code = issued.json()["code"]
        stored = await self.pool.fetchval("SELECT code_hash FROM v2_password_recoveries")
        self.assertEqual(token_hash(code), stored)
        self.assertNotIn(code, str(await self.pool.fetch("SELECT details FROM v2_audit_log")))
        self.assertEqual(200, (await self.reset(code)).status_code)
        self.assertEqual(401, (await self.client.get("/api/v1/auth/me")).status_code)
        self.assertEqual(200, (await self.client.get("/api/v1/admin/me")).status_code)
        self.assertEqual(401, (await self.reset(code)).status_code)
        old = await self.client.post("/api/v1/auth/login", json={"email": "user@example.invalid", "password": legacy.PASSWORD})
        self.assertEqual(401, old.status_code)
        new = await self.client.post("/api/v1/auth/login", json={"email": "user@example.invalid", "password": "replacement-password-123"})
        self.assertEqual(200, new.status_code)

    async def test_recovery_idempotency_never_reveals_saved_code_again(self):
        user = await self.register(); await self.admin_login(); key = uuid.uuid4()
        first = await self.issue(user["user"]["id"], key)
        second = await self.issue(user["user"]["id"], key)
        self.assertEqual(first.json()["recovery_id"], second.json()["recovery_id"])
        self.assertIsNone(second.json()["code"])
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(409, (await self.issue(user["user"]["id"], key, "其他核验原因")).status_code)
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_password_recoveries"))

    async def test_expiry_wrong_email_and_reissued_codes_rejected(self):
        user = await self.register(); await self.admin_login()
        first = await self.issue(user["user"]["id"]); second = await self.issue(user["user"]["id"])
        self.assertEqual(401, (await self.reset(first.json()["code"])).status_code)
        self.assertEqual(401, (await self.reset(second.json()["code"], "someone@example.invalid")).status_code)
        await self.pool.execute("UPDATE v2_password_recoveries SET expires_at=now()-interval '1 second'")
        self.assertEqual(401, (await self.reset(second.json()["code"])).status_code)

    async def test_recovery_cannot_target_admin_or_be_issued_by_user(self):
        await self.register()
        denied = await self.client.post(f"/api/v1/admin/users/{self.admin_id}/password-recovery", headers=self.csrf(), json={"reason": "尝试越权", "idempotency_key": str(uuid.uuid4())})
        self.assertEqual(401, denied.status_code)
        await self.admin_login()
        self.assertEqual(404, (await self.issue(self.admin_id)).status_code)

    async def test_concurrent_recovery_has_one_winner(self):
        user = await self.register(); await self.admin_login()
        issued = await self.issue(user["user"]["id"])
        responses = await asyncio.gather(self.reset(issued.json()["code"]), self.reset(issued.json()["code"]))
        self.assertEqual([200,401], sorted(response.status_code for response in responses))

    async def test_bad_recovery_attempts_are_limited(self):
        for _ in range(5):
            self.assertEqual(401, (await self.reset("x"*32)).status_code)
        limited = await self.reset("x"*32)
        self.assertEqual(429, limited.status_code)
        self.assertIn("retry-after", limited.headers)

    async def test_admin_recovery_code_routes_are_removed(self):
        await self.admin_login()
        issued = await self.client.post("/api/v1/admin/mfa/recovery-codes", headers=self.csrf(True))
        self.assertEqual(404, issued.status_code)
        self.assertEqual(404, (await self.client.post("/api/v1/admin/recovery-login", json={})).status_code)

    async def test_stale_user_can_bind_but_cannot_skip_csrf(self):
        await self.register(); await self.expire_stepup()
        bound = await self.client.put("/api/v1/accounts/leyu", headers=self.csrf(), json={"account_label":"本人", "username":"test-user", "password":"test-password"})
        self.assertEqual(200, bound.status_code, bound.text)
        self.assertEqual("unverified", bound.json()["state"])
        stopped = await self.client.put("/api/v1/accounts/leyu/auto-betting", headers=self.csrf(), json={"enabled":False})
        self.assertEqual(200, stopped.status_code, stopped.text)
        self.assertEqual(200, (await self.client.post("/api/v1/accounts/leyu/emergency-stop", headers=self.csrf())).status_code)

    async def test_export_profile_omits_all_credential_material(self):
        user = await self.register()
        await self.pool.execute("INSERT INTO v2_leyu_accounts(id,user_id,encrypted_credentials,encrypted_session,account_label) VALUES($1,$2,'DO-NOT-EXPORT-CREDS','DO-NOT-EXPORT-TOKEN','本人账户')",uuid.uuid4(),uuid.UUID(user["user"]["id"]))
        exported = await self.client.get("/api/v1/auth/export")
        self.assertEqual(200, exported.status_code, exported.text)
        self.assertNotIn("DO-NOT-EXPORT", exported.text)
        self.assertNotIn(legacy.PASSWORD, exported.text)
        self.assertNotIn(self.client.cookies[auth.USER_COOKIE], exported.text)
        self.assertEqual(user["user"]["id"], exported.json()["items"][0]["profile"]["id"])
        self.assertEqual(422, (await self.client.get("/api/v1/auth/export?section=credentials")).status_code)

    async def test_export_membership_keyset_and_foreign_rows_are_excluded(self):
        first = await self.register("first@example.invalid")
        second = await self.register()
        plan = await self.pool.fetchval("SELECT id FROM v2_membership_plans LIMIT 1")
        for _ in range(3):
            await self.pool.execute("INSERT INTO v2_memberships(id,user_id,plan_id,status,starts_at,ends_at,grant_source) VALUES($1,$2,$3,'active',now(),now()+interval '1 day','test')",uuid.uuid4(),uuid.UUID(second["user"]["id"]),plan)
        page = await self.client.get("/api/v1/auth/export?section=memberships&limit=2")
        self.assertEqual(200,page.status_code,page.text)
        rows = page.json()["items"]
        self.assertEqual(2,len(rows)); self.assertTrue(page.json()["next_cursor"])
        following = await self.client.get("/api/v1/auth/export",params={"section":"memberships","limit":2,"cursor":page.json()["next_cursor"]})
        self.assertEqual(2,len(following.json()["items"]))
        self.assertFalse(set(row["id"] for row in rows)&set(row["id"] for row in following.json()["items"]))
        self.assertEqual(422,(await self.client.get("/api/v1/auth/export?section=orders&cursor=broken")).status_code)

    async def test_closure_requires_exact_consent_without_password_step_up(self):
        await self.register()
        bad = await self.client.post("/api/v1/auth/closure",headers=self.csrf(),json={"confirmation_phrase":"yes"})
        self.assertEqual(422,bad.status_code)

    async def test_no_open_accounting_anonymizes_and_keeps_ledger_identity(self):
        user=await self.register(); uid=uuid.UUID(user["user"]["id"])
        await self.pool.execute("INSERT INTO v2_leyu_accounts(id,user_id,encrypted_credentials,encrypted_session,account_label) VALUES($1,$2,'secret-credential','secret-session','本人')",uuid.uuid4(),uid)
        closed=await self.close()
        self.assertEqual(200,closed.status_code,closed.text); self.assertEqual("anonymized",closed.json()["state"])
        record=await self.pool.fetchrow("SELECT * FROM v2_users WHERE id=$1",uid)
        self.assertEqual("已注销用户",record["display_name"]); self.assertTrue(record["anonymized_at"])
        self.assertNotEqual("user@example.invalid",record["email"])
        account=await self.pool.fetchrow("SELECT * FROM v2_leyu_accounts WHERE user_id=$1",uid)
        self.assertEqual("",account["encrypted_credentials"]); self.assertIsNone(account["encrypted_session"])
        self.assertEqual("disabled",account["health_state"])
        self.assertEqual(0,await self.pool.fetchval("SELECT count(*) FROM v2_sessions WHERE user_id=$1",uid))
        self.assertEqual(401,(await self.client.get("/api/v1/auth/me")).status_code)

    async def test_running_account_job_defers_erasure_and_rebind_is_blocked(self):
        user=await self.register(); uid=uuid.UUID(user["user"]["id"]); account=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_leyu_accounts(id,user_id,encrypted_credentials,encrypted_session,account_label) VALUES($1,$2,'keep-for-reconciliation','keep-session','本人')",account,uid)
        await self.pool.execute("INSERT INTO v2_account_jobs(id,user_id,account_id,binding_revision,kind,state) VALUES($1,$2,$3,1,'reconcile','running')",uuid.uuid4(),uid,account)
        closed=await self.close()
        self.assertEqual(202,closed.status_code,closed.text); self.assertEqual("pending_accounting",closed.json()["state"])
        self.assertEqual("keep-session",await self.pool.fetchval("SELECT encrypted_session FROM v2_leyu_accounts WHERE id=$1",account))
        self.assertEqual(200,(await self.client.get("/api/v1/auth/export")).status_code)
        self.assertEqual(409,(await self.client.put("/api/v1/accounts/leyu",headers=self.csrf(),json={"account_label":"另一人","username":"other","password":"password"})).status_code)
        self.assertEqual(200,(await self.client.put("/api/v1/accounts/leyu/auto-betting",headers=self.csrf(),json={"enabled":False})).status_code)
        await self.pool.execute("UPDATE v2_account_jobs SET state='succeeded' WHERE account_id=$1",account)
        finished=await self.close(); self.assertEqual(200,finished.status_code,finished.text)

    async def test_cancel_closure_never_restores_automatic_betting(self):
        user=await self.register(); uid=uuid.UUID(user["user"]["id"])
        await self.pool.execute("UPDATE v2_users SET closure_requested_at=now() WHERE id=$1",uid)
        await self.pool.execute("INSERT INTO v2_user_bet_settings(user_id,auto_enabled,emergency_stopped) VALUES($1,FALSE,TRUE)",uid)
        cancelled=await self.client.post("/api/v1/auth/closure/cancel",headers=self.csrf())
        self.assertEqual(200,cancelled.status_code,cancelled.text)
        policy=await self.pool.fetchrow("SELECT * FROM v2_user_bet_settings WHERE user_id=$1",uid)
        self.assertFalse(policy["auto_enabled"]); self.assertTrue(policy["emergency_stopped"])

    async def seed_order(self, user_id, state):
        source, expert, message, signal, order = [uuid.uuid4() for _ in range(5)]
        fixture="test:"+uuid.uuid4().hex
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,$2,'隔离来源')",source,str(source))
        await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,$2,'隔离专家')",expert,str(expert))
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,'1','隔离推荐样本',now())",message,source,expert)
        await self.pool.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) VALUES($1,$1,'football','隔离联赛','甲','乙',now(),'live')",fixture)
        await self.pool.execute("INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,line,selection,recommendation_price,quote_time,confidence,period,market_kind) VALUES($1,$2,$3,$4,'live','2','全场大小',2.5,'over',1.95,now(),0.9,'full_time','total')",signal,message,expert,fixture)
        await self.pool.execute("INSERT INTO v2_bet_orders(id,user_id,signal_id,idempotency_key,state,stake,price,reserved_stake,result_payload) VALUES($1,$2,$3,$4,$5,1,1.95,1,'{\"receipt\":\"accounting-evidence\"}')",order,user_id,signal,str(order),state)
        return order

    async def test_unknown_and_accepted_orders_keep_credentials_and_evidence(self):
        user=await self.register(); uid=uuid.UUID(user["user"]["id"])
        await self.pool.execute("INSERT INTO v2_leyu_accounts(id,user_id,encrypted_credentials,encrypted_session,account_label) VALUES($1,$2,'required-credential','required-session','本人')",uuid.uuid4(),uid)
        unknown=await self.seed_order(uid,"unknown"); accepted=await self.seed_order(uid,"accepted"); pending=await self.seed_order(uid,"pending")
        response=await self.close()
        self.assertEqual(202,response.status_code,response.text)
        self.assertEqual(2,response.json()["outstanding_orders"])
        self.assertEqual("required-session",await self.pool.fetchval("SELECT encrypted_session FROM v2_leyu_accounts WHERE user_id=$1",uid))
        self.assertEqual("unknown",await self.pool.fetchval("SELECT state FROM v2_bet_orders WHERE id=$1",unknown))
        self.assertEqual("accepted",await self.pool.fetchval("SELECT state FROM v2_bet_orders WHERE id=$1",accepted))
        self.assertEqual("cancelled",await self.pool.fetchval("SELECT state FROM v2_bet_orders WHERE id=$1",pending))
        self.assertEqual({"receipt":"accounting-evidence"},await self.pool.fetchval("SELECT result_payload FROM v2_bet_orders WHERE id=$1",unknown))
        policy=await self.pool.fetchrow("SELECT * FROM v2_user_bet_settings WHERE user_id=$1",uid)
        self.assertFalse(policy["auto_enabled"]); self.assertTrue(policy["emergency_stopped"])
        await self.pool.execute("UPDATE v2_bet_orders SET state='settled',reserved_stake=0,settled_net=0.95,settled_at=now() WHERE id=ANY($1::uuid[])",[unknown,accepted])
        completed=await self.close(); self.assertEqual(200,completed.status_code,completed.text)
        self.assertEqual(3,await self.pool.fetchval("SELECT count(*) FROM v2_bet_orders WHERE user_id=$1",uid))
        self.assertEqual({"receipt":"accounting-evidence"},await self.pool.fetchval("SELECT result_payload FROM v2_bet_orders WHERE id=$1",unknown))

    async def test_personal_order_export_excludes_foreign_rows_and_internal_payload(self):
        first=await self.register("first@example.invalid")
        other=await self.seed_order(uuid.UUID(first["user"]["id"]),"settled")
        user=await self.register(); uid=uuid.UUID(user["user"]["id"])
        own=await self.seed_order(uid,"unknown")
        response=await self.client.get("/api/v1/auth/export?section=orders")
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual([str(own)],[row["id"] for row in response.json()["items"]])
        self.assertNotIn(str(other),response.text)
        self.assertNotIn("accounting-evidence",response.text)

    async def test_admin_cookie_and_server_session_last_180_days(self):
        response = await self.admin_login()
        self.assert_session_cookies(response, (auth.ADMIN_COOKIE, auth.ADMIN_CSRF_COOKIE))
        session=await self.pool.fetchrow("SELECT expires_at,created_at FROM v2_sessions WHERE user_id=$1",self.admin_id)
        self.assertAlmostEqual((session["expires_at"]-session["created_at"]).total_seconds(),180*86400,delta=2)
        await self.pool.execute("UPDATE v2_sessions SET last_seen_at=now()-interval '16 minutes' WHERE user_id=$1",self.admin_id)
        self.assertEqual(200,(await self.client.get("/api/v1/admin/me")).status_code)

    def assert_session_cookies(self, response, names):
        cookies = SimpleCookie()
        for value in response.headers.get_list("set-cookie"):
            cookies.load(value)
        deadline = datetime.fromisoformat(response.json()["expires_at"])
        for name in names:
            cookie = cookies[name]
            self.assertEqual("15552000", cookie["max-age"])
            self.assertAlmostEqual(parsedate_to_datetime(cookie["expires"]).timestamp(),deadline.timestamp(),delta=1)
            self.assertEqual("strict", cookie["samesite"])
        self.assertTrue(cookies[names[0]]["httponly"])

    async def test_user_login_refresh_expiry_and_revocation_with_long_cookie(self):
        user = await self.register()
        uid = uuid.UUID(user["user"]["id"])
        response = await self.client.post("/api/v1/auth/login",json={"email":"user@example.invalid","password":legacy.PASSWORD})
        self.assertEqual(200,response.status_code,response.text)
        self.assert_session_cookies(response,(auth.USER_COOKIE,auth.CSRF_COOKIE))
        old_token = self.client.cookies[auth.USER_COOKIE]
        await self.pool.execute("UPDATE v2_sessions SET created_at=now()-interval '179 days',last_seen_at=now()-interval '30 days',expires_at=now()+interval '1 day',reauthenticated_at=now()-interval '30 days' WHERE user_id=$1",uid)
        self.assertEqual(200,(await self.client.get("/api/v1/auth/me")).status_code)
        refreshed = await self.client.post("/api/v1/auth/refresh",headers=self.csrf())
        self.assertEqual(200,refreshed.status_code,refreshed.text)
        self.assert_session_cookies(refreshed,(auth.USER_COOKIE,auth.CSRF_COOKIE))
        self.assertNotEqual(old_token,self.client.cookies[auth.USER_COOKIE])
        denied = await self.client.get("/api/v1/auth/me",headers={"Authorization":"Bearer "+old_token})
        self.assertEqual(401,denied.status_code)
        exported = await self.client.get("/api/v1/auth/export")
        self.assertEqual(200, exported.status_code, exported.text)
        await self.pool.execute("UPDATE v2_sessions SET expires_at=now()-interval '1 second' WHERE user_id=$1",uid)
        self.assertEqual(401,(await self.client.get("/api/v1/auth/me")).status_code)
        self.assertEqual(401,(await self.client.post("/api/v1/auth/refresh",headers=self.csrf())).status_code)

    async def test_audit_failure_rolls_back_recovery_and_anonymization(self):
        user=await self.register(); await self.admin_login()
        await self.pool.execute("ALTER TABLE v2_audit_log ADD CONSTRAINT lifecycle_reject CHECK(action NOT IN('password_recovery_issued','account_anonymized'))")
        with self.assertRaises(Exception):
            await self.issue(user["user"]["id"])
        self.assertEqual(0,await self.pool.fetchval("SELECT count(*) FROM v2_password_recoveries"))
        with self.assertRaises(Exception):
            await self.close()
        row=await self.pool.fetchrow("SELECT disabled_at,closure_requested_at,email FROM v2_users WHERE id=$1",uuid.UUID(user["user"]["id"]))
        self.assertIsNone(row["disabled_at"]); self.assertIsNone(row["closure_requested_at"])
        self.assertEqual("user@example.invalid",row["email"])

    async def test_password_recovery_and_change_revoke_old_betting_consent(self):
        user=await self.register(); uid=uuid.UUID(user["user"]["id"])
        await self.pool.execute("INSERT INTO v2_user_bet_settings(user_id,auto_enabled,emergency_stopped,consent_account_id) VALUES($1,TRUE,FALSE,$2)",uid,uuid.uuid4())
        pending=await self.seed_order(uid,"pending"); unknown=await self.seed_order(uid,"unknown")
        changed=await self.client.put("/api/v1/auth/password",headers=self.csrf(),json={"new_password":"new-current-password-123"})
        self.assertEqual(200,changed.status_code,changed.text); self.assertFalse(changed.json()["auto_betting"])
        policy=await self.pool.fetchrow("SELECT * FROM v2_user_bet_settings WHERE user_id=$1",uid)
        self.assertFalse(policy["auto_enabled"]); self.assertTrue(policy["emergency_stopped"]); self.assertIsNone(policy["consent_account_id"])
        self.assertEqual("cancelled",await self.pool.fetchval("SELECT state FROM v2_bet_orders WHERE id=$1",pending))
        self.assertEqual("unknown",await self.pool.fetchval("SELECT state FROM v2_bet_orders WHERE id=$1",unknown))
        await self.pool.execute("UPDATE v2_user_bet_settings SET auto_enabled=TRUE,emergency_stopped=FALSE WHERE user_id=$1",uid)
        await self.admin_login(); issued=await self.issue(uid)
        reset=await self.reset(issued.json()["code"])
        self.assertEqual(200,reset.status_code,reset.text); self.assertFalse(reset.json()["auto_betting"])
        self.assertFalse(await self.pool.fetchval("SELECT auto_enabled FROM v2_user_bet_settings WHERE user_id=$1",uid))
        self.assertEqual("unknown",await self.pool.fetchval("SELECT state FROM v2_bet_orders WHERE id=$1",unknown))

    async def test_long_admin_session_write_and_expiration(self):
        user = await self.register()
        await self.admin_login()
        await self.pool.execute("UPDATE v2_sessions SET created_at=now()-interval '179 days',expires_at=now()+interval '1 day',last_seen_at=now()-interval '1 day' WHERE user_id=$1",self.admin_id)
        self.assertEqual(200,(await self.client.get("/api/v1/admin/me")).status_code)
        self.assertEqual(201,(await self.issue(user["user"]["id"])).status_code)
        await self.expire_stepup("admin")
        self.assertEqual(201,(await self.issue(user["user"]["id"])).status_code)
        await self.pool.execute("UPDATE v2_sessions SET expires_at=now()-interval '1 second' WHERE user_id=$1",self.admin_id)
        self.assertEqual(401,(await self.client.get("/api/v1/admin/me")).status_code)

    async def test_revoked_session_cannot_bind_after_waiting_for_user_lock(self):
        from unittest.mock import patch
        from app.routes import accounts
        user=await self.register(); uid=uuid.UUID(user["user"]["id"])
        captured=None
        original=accounts.require_user
        async def delayed(request):
            nonlocal captured
            captured=await original(request)
            await self.pool.execute("DELETE FROM v2_sessions WHERE user_id=$1",uid)
            return captured
        with patch.object(accounts,"require_user",side_effect=delayed):
            response=await self.client.put("/api/v1/accounts/leyu",headers=self.csrf(),json={"account_label":"本人","username":"test","password":"test"})
        self.assertEqual(401,response.status_code,response.text)
        self.assertEqual(0,await self.pool.fetchval("SELECT count(*) FROM v2_leyu_accounts WHERE user_id=$1",uid))
