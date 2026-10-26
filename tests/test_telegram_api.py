from datetime import datetime, timedelta, timezone
import hashlib
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import asyncpg
from fastapi import FastAPI
import httpx

from app import auth, database
from app.routes import telegram
from app.security import decrypt_secret, token_hash


TEST_URL = os.environ.get("TELEGRAM_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL, "需要显式临时 Telegram 数据库；不回落生产配置")
class TelegramApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.name = "telegram_" + uuid.uuid4().hex
        self.control = await asyncpg.connect(TEST_URL)
        await self.control.execute(f'CREATE DATABASE "{self.name}"')
        url = TEST_URL.rsplit("/", 1)[0] + "/" + self.name
        self.pool = await database.open_pool(SimpleNamespace(database_url=url), migrate=True)
        self.app = FastAPI()
        self.app.state.settings = SimpleNamespace(collector_token="test-collector-token", encryption_key=bytes(range(32)))
        self.app.include_router(telegram.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver",
                                        headers={"Authorization": "Bearer test-collector-token"})
        self.source_id, self.expert_id, self.admin_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'-1001','测试来源')", self.source_id)
        await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'7','测试专家')", self.expert_id)
        await self.pool.execute("INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)", self.source_id, self.expert_id)
        self.owner = uuid.uuid4()
        claim = await self.client.post("/internal/telegram/checkpoints/claim", json={"chat_id": "-1001", "owner": str(self.owner)})
        self.assertEqual(200, claim.status_code, claim.text)

    async def asyncTearDown(self):
        await self.client.aclose()
        await database.close_pool()
        await self.control.execute(f'DROP DATABASE "{self.name}"')
        await self.control.close()

    def event(self, *, version="v1", text="赛前阿森纳大2.5", media=False, kind="message", minutes=0):
        when = datetime(2026, 10, 26, tzinfo=timezone.utc) + timedelta(minutes=minutes)
        return {"chat_id": "-1001", "owner": str(self.owner), "platform_message_id": "11", "member_message_ids": [11],
                "version_key": hashlib.sha256(version.encode()).hexdigest(), "event_kind": kind,
                "sender_id": "7", "sender_nickname": "测试专家", "source_version_at": when.isoformat(),
                "posted_at": datetime(2026, 10, 26, tzinfo=timezone.utc).isoformat(), "raw_text": text,
                "media_state": "pending" if media else "ready",
                "media_manifest": [{"message_id": 11, "mime_type": "image/jpeg", "bytes": 100}] if media else [],
                "media_objects": []}

    async def post(self, body):
        return await self.client.post("/internal/telegram/events", json=body)

    async def admin_client(self):
        await self.pool.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,'test-admin@example.invalid','管理员','unused','admin')", self.admin_id)
        await self.pool.execute("INSERT INTO v2_sessions(token_hash,csrf_hash,user_id,session_kind,expires_at) VALUES($1,$2,$3,'admin',now()+interval '1 hour')",
                                token_hash("test-admin-session"), token_hash("test-admin-csrf"), self.admin_id)
        self.client.headers.pop("Authorization", None)
        self.client.cookies.set(auth.ADMIN_COOKIE, "test-admin-session")
        self.client.cookies.set(auth.ADMIN_CSRF_COOKIE, "test-admin-csrf")
        self.client.headers["X-CSRF-Token"] = "test-admin-csrf"

    async def test_pending_failed_ready_is_durable_and_job_once(self):
        body = self.event(media=True)
        pending = await self.post(body)
        self.assertEqual(200, pending.status_code, pending.text)
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_analysis_jobs"))
        self.assertEqual("赛前阿森纳大2.5", await self.pool.fetchval("SELECT raw_text FROM v2_expert_messages"))
        body.update(media_state="failed", last_error="TimeoutError")
        failed = await self.post(body)
        self.assertEqual(200, failed.status_code, failed.text)
        retry = await self.client.get("/internal/telegram/pending")
        self.assertEqual(1, len(retry.json()["items"]))
        body.update(media_state="ready", last_error=None,
                    media_objects=[{"object_key": "telegram/test/11", "mime_type": "image/jpeg", "sha256": "a" * 64, "bytes": 100}])
        ready = await self.post(body)
        repeated = await self.post(body)
        self.assertEqual(200, ready.status_code, ready.text)
        self.assertEqual(ready.json()["message_id"], repeated.json()["message_id"])
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_analysis_jobs"))
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_expert_messages"))
        self.assertEqual([], (await self.client.get("/internal/telegram/pending")).json()["items"])

    async def test_edit_withdraws_previous_job_and_keeps_original(self):
        first = await self.post(self.event())
        second = await self.post(self.event(version="v2", text="改为小2.5", kind="edit", minutes=1))
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual(2, await self.pool.fetchval("SELECT count(*) FROM v2_expert_messages"))
        old = await self.pool.fetchrow("SELECT raw_text,deleted_at FROM v2_expert_messages WHERE id=$1", uuid.UUID(first.json()["message_id"]))
        self.assertEqual("赛前阿森纳大2.5", old["raw_text"])
        self.assertIsNotNone(old["deleted_at"])
        self.assertEqual("failed", await self.pool.fetchval("SELECT state FROM v2_analysis_jobs WHERE message_id=$1", uuid.UUID(first.json()["message_id"])))
        self.assertEqual("superseded", (await self.post(self.event())).json()["state"])
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_telegram_events WHERE is_current"))

    async def test_delete_tombstone_rejects_late_replay(self):
        original = await self.post(self.event())
        deleted = self.event(version="deleted", text="", kind="delete", minutes=2)
        deleted["platform_message_id"] = "delete:test"
        self.assertEqual("deleted", (await self.post(deleted)).json()["state"])
        replay = await self.post(self.event(version="late-edit", text="迟到编辑", kind="edit", minutes=3))
        self.assertEqual("deleted", replay.json()["state"])
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_expert_messages"))
        self.assertIsNotNone(await self.pool.fetchval("SELECT deleted_at FROM v2_expert_messages WHERE id=$1", uuid.UUID(original.json()["message_id"])))

    async def test_checkpoint_lease_and_compare_and_swap(self):
        conflict = await self.client.post("/internal/telegram/checkpoints/claim", json={"chat_id": "-1001", "owner": str(uuid.uuid4())})
        self.assertEqual(409, conflict.status_code)
        advance = {"chat_id": "-1001", "owner": str(self.owner), "last_message_id": 35, "previous_message_id": 0}
        self.assertEqual(200, (await self.client.put("/internal/telegram/checkpoints", json=advance)).status_code)
        self.assertEqual(409, (await self.client.put("/internal/telegram/checkpoints", json=advance)).status_code)
        advance.update(last_message_id=34, previous_message_id=35)
        self.assertEqual(422, (await self.client.put("/internal/telegram/checkpoints", json=advance)).status_code)
        await self.pool.execute("UPDATE v2_source_checkpoints SET lease_until=now()-interval '1 second'")
        self.assertEqual(409, (await self.post(self.event())).status_code)

    async def test_untrusted_sender_and_service_token_rejected(self):
        body = self.event()
        body["sender_id"] = "8"
        self.assertEqual(403, (await self.post(body)).status_code)
        self.client.headers["Authorization"] = "Bearer wrong"
        self.assertEqual(401, (await self.client.get("/internal/telegram/sources")).status_code)

    async def test_conflicting_version_rejected_nickname_updated_without_duplication(self):
        await self.post(self.event())
        changed = self.event(text="被替换的同版本内容")
        self.assertEqual(409, (await self.post(changed)).status_code)
        renamed = self.event()
        renamed["sender_nickname"] = "专家新昵称"
        self.assertEqual(200, (await self.post(renamed)).status_code)
        self.assertEqual("专家新昵称", await self.pool.fetchval("SELECT display_nickname FROM v2_experts WHERE id=$1", self.expert_id))
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_expert_messages"))

    async def test_incomplete_media_cannot_be_queued(self):
        body = self.event(media=True)
        body["media_state"] = "ready"
        self.assertEqual(422, (await self.post(body)).status_code)
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_expert_messages"))
        body["media_state"] = "unsupported"
        self.assertEqual(200, (await self.post(body)).status_code)
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_analysis_jobs"))

    async def test_newest_version_rejects_out_of_order_older_edit(self):
        await self.post(self.event(version="newest", minutes=10, kind="edit"))
        old = await self.post(self.event(version="old", minutes=5, kind="edit"))
        self.assertEqual("ignored_stale", old.json()["state"])
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_expert_messages"))

    async def test_observed_reconciliation_rotates_past_first_page(self):
        for number in range(1, 24):
            body = self.event(version=f"message-{number}")
            body.update(platform_message_id=str(number), member_message_ids=[number])
            self.assertEqual(200, (await self.post(body)).status_code)
        await self.pool.execute("UPDATE v2_telegram_events SET next_check_at=now()-interval '1 minute'")
        observed = []
        for _ in range(3):
            page = await self.client.post("/internal/telegram/observed", json={"chat_id": "-1001", "owner": str(self.owner), "limit": 10})
            self.assertEqual(200, page.status_code, page.text)
            observed.extend(item["id"] for item in page.json()["items"])
        self.assertEqual(23, len(set(observed)))

    async def test_album_member_deletion_does_not_recommend_incomplete_album(self):
        body = self.event(media=True)
        body.update(platform_message_id="album:77", member_message_ids=[10, 11], media_state="unsupported")
        self.assertEqual(200, (await self.post(body)).status_code)
        deletion = self.event(kind="delete", text="", version="delete-member", minutes=1)
        deletion.update(platform_message_id="delete:11", member_message_ids=[11])
        self.assertEqual(200, (await self.post(deletion)).status_code)
        body.update(version_key=hashlib.sha256(b"partial-album").hexdigest(), member_message_ids=[10])
        self.assertEqual("deleted", (await self.post(body)).json()["state"])

    async def test_full_original_reading_pagination_with_identical_timestamps(self):
        ids = []
        for number in range(1, 6):
            body = self.event(version=f"same-date-{number}")
            body.update(platform_message_id=str(number), member_message_ids=[number])
            ids.append((await self.post(body)).json()["message_id"])
        await self.admin_client()
        observed, cursor = [], None
        for _ in range(3):
            params = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = (await self.client.get("/api/v1/admin/telegram/messages", params=params)).json()
            observed.extend(item["id"] for item in page["items"])
            cursor = page["next_cursor"]
        self.assertEqual(set(ids), set(observed))
        self.assertEqual(5, len(observed))
        detail = await self.client.get("/api/v1/admin/telegram/messages/" + ids[0])
        self.assertEqual("赛前阿森纳大2.5", detail.json()["message"]["raw_text"])
        self.assertEqual([], detail.json()["signals"])
        self.assertEqual("queued", detail.json()["analysis"][0]["state"])

    async def test_session_saved_encrypted_with_safe_account_only(self):
        session = "test-session-only-" * 5
        response = await self.client.put("/internal/telegram/session", json={"session": session, "account": {"id": "1234", "display_name": "测试用户", "phone": "must-not-save"}})
        self.assertEqual(200, response.status_code, response.text)
        rows = await self.pool.fetch("SELECT setting_key,encrypted_value FROM v2_admin_settings")
        decrypted = {row["setting_key"]: decrypt_secret(bytes(range(32)), row["encrypted_value"]) for row in rows}
        self.assertEqual(session, decrypted["telegram_session"])
        self.assertNotIn("must-not-save", decrypted["telegram_account"])
        self.assertNotIn(session, str(rows))

    async def test_admin_password_proxy_does_not_save_password(self):
        await self.admin_client()
        original_client = httpx.AsyncClient
        received = []

        def handler(request):
            received.append(request.content)
            return httpx.Response(200, json={"state": "authorized"})

        def factory(**kwargs):
            return original_client(**kwargs, transport=httpx.MockTransport(handler))

        with patch("app.routes.telegram.httpx.AsyncClient", factory):
            response = await self.client.post("/api/v1/admin/telegram/auth/password", json={"attempt_id": str(uuid.uuid4()), "password": "test-memory-only-password"})
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn(b"test-memory-only-password", received[0])
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_admin_settings"))
        self.assertNotIn("test-memory-only-password", str(await self.pool.fetch("SELECT details FROM v2_audit_log")))

    async def test_admin_message_history_shows_failed_original(self):
        body = self.event(media=True)
        body.update(media_state="failed", last_error="TimeoutError")
        await self.post(body)
        self.assertEqual(401, (await self.client.get("/api/v1/admin/telegram/messages")).status_code)
        await self.admin_client()
        response = await self.client.get("/api/v1/admin/telegram/messages")
        self.assertEqual(200, response.status_code)
        self.assertEqual("failed", response.json()["items"][0]["media_state"])
        self.assertEqual("赛前阿森纳大2.5", response.json()["items"][0]["raw_text"])


if __name__ == "__main__":
    unittest.main()
