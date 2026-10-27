"""Real temporary PostgreSQL contracts for Telegram directories and manual source trust."""
import json
import unittest
from unittest.mock import patch
import uuid

import httpx

from app.routes import admin
import test_telegram_api as legacy


@unittest.skipUnless(legacy.TEST_URL, "需要显式临时数据库；禁止回落到生产配置")
class TelegramSourceTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = legacy.TelegramApiTests.asyncTearDown
    admin_client = legacy.TelegramApiTests.admin_client

    async def asyncSetUp(self):
        await legacy.TelegramApiTests.asyncSetUp(self)
        self.app.include_router(admin.router)
        await self.admin_client()

    def fake_control(self, result, received):
        original = httpx.AsyncClient

        def handler(request):
            received.append({"path": request.url.path, "body": json.loads(request.content), "authorization": request.headers.get("Authorization")})
            return httpx.Response(200, json=result)

        def factory(**kwargs):
            return original(**kwargs, transport=httpx.MockTransport(handler))
        return patch("app.routes.telegram.httpx.AsyncClient", factory)

    async def test_directory_search_and_cursor_are_forwarded_with_available_public_state(self):
        received = []
        with self.fake_control({"state": "ready", "items": [{"chat_id": "-1001358434454", "title": "观察群"}], "next_cursor": "50"}, received):
            response = await self.client.get("/api/v1/admin/telegram/dialogs", params={"q": " 观察 ", "cursor": "50"})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("available", response.json()["state"])
        self.assertEqual({"cursor": "50", "q": "观察"}, received[0]["body"])
        self.assertEqual("Bearer test-collector-token", received[0]["authorization"])
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_audit_log"))

    async def test_admin_pagination_and_failure_categories_remain_visible(self):
        received = []
        with self.fake_control({"state": "ready", "items": [{"sender_id": "7", "nickname": "专家"}], "next_cursor": "100", "manual_id_allowed": True}, received):
            response = await self.client.get("/api/v1/admin/telegram/admins", params={"chat_id": "-1001890322660", "cursor": "50", "q": " 专家 "})
        self.assertEqual("available", response.json()["state"])
        self.assertEqual({"chat_id": "-1001890322660", "cursor": "50", "q": "专家"}, received[0]["body"])
        for result in ({"state": "unavailable", "reason": "member_enumeration_restricted", "items": [], "manual_id_allowed": True},
                       {"state": "flood_wait", "retry_after": 42, "items": []}):
            with self.fake_control(result, []):
                response = await self.client.get("/api/v1/admin/telegram/admins", params={"chat_id": "-1001890322660"})
            self.assertEqual(result, response.json())

    async def test_invalid_directory_inputs_are_rejected_without_control_requests(self):
        received = []
        with self.fake_control({"state": "ready", "items": []}, received):
            for params in ({"cursor": "-1"}, {"cursor": "1000001"}, {"q": "a\nb"}, {"q": "a" * 161}):
                self.assertEqual(422, (await self.client.get("/api/v1/admin/telegram/dialogs", params=params)).status_code)
            for chat_id in ("1358434454", "昵称", "-0", "@ab", "-9223372036854775809"):
                self.assertEqual(422, (await self.client.get("/api/v1/admin/telegram/admins", params={"chat_id": chat_id})).status_code)
            self.assertEqual(422, (await self.client.get("/api/v1/admin/telegram/admins", params={"chat_id": "-1001", "q": "a\nb"})).status_code)
        self.assertEqual([], received)

    async def test_directory_invalid_response_and_non_admin_are_not_reported_as_empty_success(self):
        with self.fake_control({"state": "ready", "items": "invalid"}, []):
            response = await self.client.get("/api/v1/admin/telegram/dialogs")
        self.assertEqual(503, response.status_code)
        self.client.cookies.clear()
        received = []
        with self.fake_control({"state": "ready", "items": []}, received):
            self.assertEqual(401, (await self.client.get("/api/v1/admin/telegram/dialogs")).status_code)
        self.assertEqual([], received)

    async def test_source_ids_are_canonical_unique_and_cannot_be_retargeted(self):
        response = await self.client.post("/api/v1/admin/sources", json={"chat_id": " -001002 ", "display_name": " 新来源 "})
        self.assertEqual(201, response.status_code, response.text)
        source = response.json()
        self.assertEqual("-1002", source["chat_id"])
        self.assertEqual("新来源", source["display_name"])
        self.assertEqual(409, (await self.client.post("/api/v1/admin/sources", json={"chat_id": "-1002", "display_name": "重名"})).status_code)
        changed = await self.client.put(f"/api/v1/admin/sources/{source['id']}", json={"chat_id": "-1003", "display_name": "不能换群"})
        self.assertEqual(409, changed.status_code)
        self.assertEqual("-1002", await self.pool.fetchval("SELECT platform_chat_id FROM v2_expert_sources WHERE id=$1", uuid.UUID(source["id"])))

    async def test_empty_and_non_numeric_identity_input_cannot_create_trust(self):
        for chat_id, title in (("@expertnews", "群"), ("1001", "群"), ("-0", "群"), ("-1002", "  ")):
            self.assertEqual(422, (await self.client.post("/api/v1/admin/sources", json={"chat_id": chat_id, "display_name": title})).status_code)
        for sender, nickname in (("昵称", "作者"), ("@expertnews", "作者"), ("0", "作者"), ("7", " "), ("7", "错\n误")):
            self.assertEqual(422, (await self.client.post(f"/api/v1/admin/sources/{self.source_id}/experts", json={"sender_id": sender, "nickname": nickname})).status_code)
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_experts"))

    async def test_manual_trust_is_audited_with_source_and_does_not_claim_admin_verification(self):
        response = await self.client.post(f"/api/v1/admin/sources/{self.source_id}/experts", json={"sender_id": " 0007 ", "nickname": "新昵称", "verification_note": "管理员已核验消息作者编号"})
        self.assertEqual(201, response.status_code, response.text)
        self.assertEqual(str(self.expert_id), response.json()["id"])
        self.assertEqual("7", response.json()["sender_id"])
        self.assertEqual("manual", response.json()["trust_basis"])
        self.assertFalse(response.json()["telegram_admin_verified"])
        details = await self.pool.fetchval("SELECT details FROM v2_audit_log WHERE action='telegram_expert_trusted'")
        self.assertEqual(str(self.source_id), details["source_id"])
        self.assertEqual("manual", details["trust_basis"])
        self.assertEqual("管理员已核验消息作者编号", details["verification_note"])
        configured = (await self.client.get(f"/api/v1/admin/sources/{self.source_id}/experts")).json()["items"][0]
        self.assertEqual(str(self.source_id), configured["source_id"])
        self.assertFalse(configured["telegram_admin_verified"])

    async def test_same_author_mapping_and_source_disabling_are_independent(self):
        second = (await self.client.post("/api/v1/admin/sources", json={"chat_id": "-1002", "display_name": "另一来源"})).json()
        added = await self.client.post(f"/api/v1/admin/sources/{second['id']}/experts", json={"sender_id": "7", "nickname": "专家同一身份"})
        self.assertEqual(201, added.status_code, added.text)
        self.assertEqual(str(self.expert_id), added.json()["id"])
        disabled = await self.client.delete(f"/api/v1/admin/sources/{self.source_id}/experts/{self.expert_id}")
        self.assertEqual(200, disabled.status_code, disabled.text)
        self.assertTrue(await self.pool.fetchval("SELECT enabled FROM v2_source_experts WHERE source_id=$1 AND expert_id=$2", uuid.UUID(second["id"]), self.expert_id))
        await self.client.put(f"/api/v1/admin/sources/{self.source_id}", json={"chat_id": "-1001", "display_name": "原来源", "enabled": False})
        response = await self.client.get("/internal/telegram/sources", headers={"Authorization": "Bearer test-collector-token"})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(["-1002"], [item["chat_id"] for item in response.json()["items"]])
        self.assertEqual(["7"], response.json()["items"][0]["trusted_sender_ids"])

    async def test_identical_nickname_never_merges_distinct_stable_authors(self):
        for sender in ("7", "8"):
            response = await self.client.post(f"/api/v1/admin/sources/{self.source_id}/experts", json={"sender_id": sender, "nickname": "完全同名"})
            self.assertEqual(201, response.status_code)
        rows = await self.pool.fetch("SELECT id,platform_sender_id FROM v2_experts WHERE display_nickname='完全同名'")
        self.assertEqual({"7", "8"}, {row["platform_sender_id"] for row in rows})
        self.assertEqual(2, len({row["id"] for row in rows}))

    async def test_trust_mapping_audit_failure_rolls_back_new_author_and_mapping(self):
        await self.pool.execute("ALTER TABLE v2_audit_log ADD CONSTRAINT block_trust_audit CHECK(action<>'telegram_expert_trusted')")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url="http://testserver", cookies=self.client.cookies, headers={"X-CSRF-Token": "test-admin-csrf"}) as client:
            response = await client.post(f"/api/v1/admin/sources/{self.source_id}/experts", json={"sender_id": "99", "nickname": "事务回滚样本"})
        self.assertEqual(500, response.status_code)
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_experts WHERE platform_sender_id='99'"))

    async def test_negative_posting_peer_is_distinct_from_a_named_individual_admin(self):
        response = await self.client.post(f"/api/v1/admin/sources/{self.source_id}/experts", json={"sender_id": "-1001358434454", "nickname": "频道发言身份", "verification_note": "按发言频道身份信任，无法区分背后的匿名管理员"})
        self.assertEqual(201, response.status_code, response.text)
        self.assertEqual("chat_sender", response.json()["sender_identity_kind"])
        self.assertFalse(response.json()["individual_author_resolved"])
        self.assertFalse(response.json()["telegram_admin_verified"])
        details = await self.pool.fetchval("SELECT details FROM v2_audit_log WHERE action='telegram_expert_trusted'")
        self.assertEqual("chat_sender", details["sender_identity_kind"])
        self.assertFalse(details["individual_author_resolved"])
