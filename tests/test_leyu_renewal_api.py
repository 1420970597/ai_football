import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import asyncpg
from fastapi import FastAPI
import httpx

from services.api.app import database
from services.api.app.database import apply_migrations
from services.api.app.routes import renewal
from services.api.app.security import decrypt_secret


@unittest.skipUnless(os.environ.get("LEYU_RENEWAL_TEST_DSN"), "需要显式隔离数据库连接，禁止生产默认值")
class RenewalAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 在连接与任何DDL前拒绝生产或其它任务数据库，测试仅运行于具名临时Docker服务。
        dsn = os.environ["LEYU_RENEWAL_TEST_DSN"]
        parts = urlsplit(dsn)
        if (parts.hostname != "ai-football-v2-pgtest" or parts.path != "/renewal_protocol_test"
                or parts.username != "renewal_protocol_test"):
            raise RuntimeError("续期测试仅允许本任务专用Docker数据库")
        async def init(conn):
            for name in ("json", "jsonb"):
                await conn.set_type_codec(name, schema="pg_catalog", encoder=json.dumps, decoder=json.loads)
        self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, init=init)
        root = Path(__file__).resolve().parents[1]
        async with self.pool.acquire() as conn:
            await apply_migrations(conn, root / 'services/api/migrations')
            await conn.execute("TRUNCATE v2_renewal_accounts,v2_renewal_attempts,v2_admin_settings,v2_audit_log CASCADE")
            await conn.execute("UPDATE v2_configuration_revisions SET revision=1 WHERE namespace='leyu'")
        self.previous_pool = database.pool
        database.pool = self.pool
        app = FastAPI()
        app.state.settings = SimpleNamespace(collector_token="isolated-collector-token", encryption_key=b"k" * 32)
        app.include_router(renewal.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://platform.invalid",
                                       headers={"Authorization": "Bearer isolated-collector-token"})
        self.account = "a" * 64

    async def asyncTearDown(self):
        await self.client.aclose()
        database.pool = self.previous_pool
        await self.pool.close()

    async def claim(self, account=None):
        return await self.client.post("/internal/collector/renewal/claim", json={"account_hash": account or self.account, "expected_version": 1})

    async def test_claim_persist_encrypted_config_and_finish_idempotent(self):
        response = await self.claim()
        self.assertEqual(201, response.status_code, response.text)
        attempt = response.json()["attempt_id"]
        response = await self.client.put("/internal/collector/renewal/config", json={"attempt_id": attempt, "expected_version": 1,
            "values": {"leyu_request_id": "private-new-session", "leyu_api_base": "https://business.invalid"}})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(2, response.json()["version"])
        stored = await self.pool.fetchval("SELECT encrypted_value FROM v2_admin_settings WHERE setting_key='leyu_request_id'")
        self.assertNotIn("private-new-session", stored)
        self.assertEqual("private-new-session", decrypt_secret(b"k" * 32, stored))
        loaded = await self.client.get("/internal/collector/renewal/config")
        self.assertEqual("private-new-session", loaded.json()["items"]["leyu_request_id"])
        finish_url = f"/internal/collector/renewal/{attempt}/finish"
        for expected in (False, True):
            done = await self.client.post(finish_url, json={"status": "success", "metadata": {"login_submitted": False}})
            self.assertEqual(200, done.status_code, done.text)
            self.assertEqual(expected, done.json()["idempotent"])
        audits = await self.pool.fetch("SELECT details FROM v2_audit_log")
        self.assertNotIn("private-new-session", json.dumps([dict(x) for x in audits]))

    async def test_concurrent_claims_only_one_attempt(self):
        import asyncio
        results = await asyncio.gather(*(self.claim() for _ in range(5)))
        self.assertEqual(1, sum(r.status_code == 201 for r in results))
        self.assertEqual(4, sum(r.status_code == 409 for r in results))
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_renewal_attempts"))

    async def test_different_account_hash_cannot_bypass_global_collector_lock(self):
        first = await self.claim()
        self.assertEqual(201, first.status_code)
        second = await self.claim("b" * 64)
        self.assertEqual(409, second.status_code)
        attempt = first.json()["attempt_id"]
        await self.client.post(f"/internal/collector/renewal/{attempt}/finish", json={"status": "unknown", "metadata": {"login_submitted": True}})
        self.assertEqual(409, (await self.claim("c" * 64)).status_code)

    async def test_unknown_or_rejected_blocks_future_login_until_reauthorization(self):
        attempt = (await self.claim()).json()["attempt_id"]
        done = await self.client.post(f"/internal/collector/renewal/{attempt}/finish", json={"status": "rejected", "metadata": {"code": "6031", "login_submitted": True}})
        self.assertEqual(200, done.status_code, done.text)
        self.assertEqual(409, (await self.claim()).status_code)
        admin = uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,$2,'test','hash','admin')", admin, f"{admin}@invalid.test")
        with patch.object(renewal, "require_admin", AsyncMock(return_value={"id": admin})):
            reset = await self.client.post(f"/api/v1/admin/leyu/renewal/{self.account}/reauthorize", json={"reason": "已经核对官方状态，重新授权测试"})
        self.assertEqual(200, reset.status_code, reset.text)
        self.assertEqual(201, (await self.claim()).status_code)

    async def test_expired_attempt_becomes_durable_unknown(self):
        attempt = (await self.claim()).json()["attempt_id"]
        await self.pool.execute("UPDATE v2_renewal_accounts SET lease_until=now()-interval '1 second' WHERE account_hash=$1", self.account)
        self.assertEqual(409, (await self.claim()).status_code)
        self.assertEqual("unknown", await self.pool.fetchval("SELECT state FROM v2_renewal_accounts WHERE account_hash=$1", self.account))
        self.assertEqual("unknown", await self.pool.fetchval("SELECT state FROM v2_renewal_attempts WHERE id=$1", uuid.UUID(attempt)))

    async def test_late_success_cannot_override_expired_lease(self):
        attempt = (await self.claim()).json()["attempt_id"]
        await self.pool.execute("UPDATE v2_renewal_accounts SET lease_until=now()-interval '1 second' WHERE account_hash=$1", self.account)
        done = await self.client.post(f"/internal/collector/renewal/{attempt}/finish", json={"status": "success"})
        self.assertEqual(409, done.status_code)
        self.assertEqual("unknown", await self.pool.fetchval("SELECT state FROM v2_renewal_accounts WHERE account_hash=$1", self.account))
        self.assertEqual("unknown", await self.pool.fetchval("SELECT state FROM v2_renewal_attempts WHERE id=$1", uuid.UUID(attempt)))

    async def test_admin_can_explicitly_reauthorize_abandoned_expired_attempt(self):
        attempt = (await self.claim()).json()["attempt_id"]
        await self.pool.execute("UPDATE v2_renewal_accounts SET lease_until=now()-interval '1 second' WHERE account_hash=$1", self.account)
        admin = uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,$2,'test','hash','admin')", admin, f"{admin}@invalid.test")
        with patch.object(renewal, "require_admin", AsyncMock(return_value={"id": admin})):
            result = await self.client.post(f"/api/v1/admin/leyu/renewal/{self.account}/reauthorize", json={"reason": "已人工核对被中断的登录结果，重新授权"})
        self.assertEqual(200, result.status_code)
        self.assertEqual("unknown", await self.pool.fetchval("SELECT state FROM v2_renewal_attempts WHERE id=$1", uuid.UUID(attempt)))
        self.assertEqual(201, (await self.claim()).status_code)

    async def test_hot_config_revision_conflict_does_not_overwrite(self):
        attempt = (await self.claim()).json()["attempt_id"]
        await self.pool.execute("UPDATE v2_configuration_revisions SET revision=2 WHERE namespace='leyu'")
        rejected = await self.client.put("/internal/collector/renewal/config", json={"attempt_id": attempt, "expected_version": 1,
            "values": {"leyu_request_id": "stale-session"}})
        self.assertEqual(409, rejected.status_code)
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_admin_settings"))

    async def test_unapproved_config_keys_and_http_domain_rejected(self):
        attempt = (await self.claim()).json()["attempt_id"]
        for values in ({"auto_betting_enabled": "true"}, {"leyu_api_base": "http://unsafe.invalid"}, {"leyu_request_id": "header\ninjection"}):
            response = await self.client.put("/internal/collector/renewal/config", json={"attempt_id": attempt, "expected_version": 1, "values": values})
            self.assertEqual(422, response.status_code)

    async def test_completed_attempt_cannot_change_config_or_terminal_result(self):
        attempt = (await self.claim()).json()["attempt_id"]
        url = f"/internal/collector/renewal/{attempt}/finish"
        self.assertEqual(200, (await self.client.post(url, json={"status": "failed"})).status_code)
        self.assertEqual(409, (await self.client.post(url, json={"status": "success"})).status_code)
        result = await self.client.put("/internal/collector/renewal/config", json={"attempt_id": attempt, "expected_version": 1, "values": {"leyu_request_id": "late"}})
        self.assertEqual(409, result.status_code)

    async def test_wrong_service_token_and_admin_reset_not_public(self):
        result = await self.client.get("/internal/collector/renewal/config", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(401, result.status_code)
        result = await self.client.post(f"/api/v1/admin/leyu/renewal/{self.account}/reauthorize", json={"reason": "未授权重置不应被接受"})
        self.assertEqual(401, result.status_code)


if __name__ == "__main__":
    unittest.main()
