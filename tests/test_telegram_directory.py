"""Telegram directory behavior using actual TL entity/role types and fake RPCs only."""
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from telethon import utils
from telethon.errors import ChatAdminRequiredError, ChannelPrivateError, UserNotParticipantError, FloodWaitError
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.types import (Channel, ChannelForbidden, ChannelParticipantAdmin, ChannelParticipantCreator,
    ChannelParticipantsAdmins, Chat, ChatAdminRights, ChatParticipant, ChatParticipantAdmin,
    ChatParticipantCreator, ChatParticipants, ChatParticipantsForbidden, ChatPhotoEmpty, InputChannel, User)

from collector.telegram import TelegramCollector
import test_telegram_collector as legacy


NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def group(identifier, title="专家讨论群", *, channel=False, username=None):
    if channel:
        return Channel(id=identifier, title=title, photo=ChatPhotoEmpty(), date=NOW, megagroup=True,
                       access_hash=123, username=username)
    return Chat(id=identifier, title=title, photo=ChatPhotoEmpty(), participants_count=120, date=NOW, version=1)


def dialog(entity):
    return SimpleNamespace(entity=entity, name=entity.title, is_group=True, is_channel=isinstance(entity, Channel))


class DirectoryClient(legacy.Client):
    def __init__(self):
        super().__init__()
        self.entities, self.dialogs, self.rpc_calls = {}, [], []
        self.roles, self.users = [], []
        self.forbidden, self.error, self.resolve_error = False, None, None
        self.page_cap = 200
        self.repeat_page = False
        self.fail_at = None

    async def get_entity(self, value):
        if self.resolve_error:
            raise self.resolve_error
        if str(value) not in self.entities:
            raise ValueError("not cached")
        return self.entities[str(value)]

    async def iter_dialogs(self, limit):
        assert limit is None
        for item in self.dialogs:
            yield item

    async def __call__(self, request):
        self.rpc_calls.append(request)
        if self.error:
            raise self.error
        if isinstance(request, GetParticipantsRequest):
            assert isinstance(request.filter, ChannelParticipantsAdmins)
            if request.offset == self.fail_at:
                raise FloodWaitError(None, capture=12)
            start = 0 if self.repeat_page else request.offset
            return SimpleNamespace(participants=self.roles[start:start + min(request.limit, self.page_cap)], users=self.users, count=len(self.roles))
        if isinstance(request, GetFullChatRequest):
            members = ChatParticipantsForbidden(request.chat_id) if self.forbidden else ChatParticipants(request.chat_id, self.roles, 1)
            return SimpleNamespace(full_chat=SimpleNamespace(participants=members), users=self.users)
        raise AssertionError("Unexpected fake RPC")


class TelegramDirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = DirectoryClient()
        self.collector = TelegramCollector(legacy.CONFIG, backend=legacy.Backend(), client_factory=lambda *args: self.client)

    async def asyncTearDown(self):
        await self.collector.disconnect()

    def use_group(self, entity):
        self.client.entities[str(utils.get_peer_id(entity))] = entity
        return str(utils.get_peer_id(entity))

    async def test_search_runs_over_all_joined_groups_and_excludes_private_chats(self):
        self.client.dialogs = [dialog(group(index, f"普通群{index}")) for index in range(1, 107)]
        target = group(1358434454, "滚球赛前观察", channel=True, username="ExpertNews")
        self.client.dialogs.append(dialog(target))
        self.client.dialogs.append(SimpleNamespace(entity=User(id=7, first_name="滚球"), name="滚球", is_group=False, is_channel=False))
        for term in ("滚球", "1358434454", "expertNEWS", "@ExpertNews"):
            result = await self.collector.control("/dialogs", {"q": term})
            self.assertEqual("ready", result["state"], result)
            self.assertEqual(["-1001358434454"], [item["chat_id"] for item in result["items"]])
            self.assertTrue(result["joined_only"])

    async def test_dialog_pages_are_stable_when_telegram_recency_order_changes(self):
        self.client.dialogs = [dialog(group(index)) for index in range(1, 122)]
        first = await self.collector.control("/dialogs", {})
        self.client.dialogs.reverse()
        second = await self.collector.control("/dialogs", {"cursor": first["next_cursor"]})
        third = await self.collector.control("/dialogs", {"cursor": second["next_cursor"]})
        ids = [item["chat_id"] for page in (first, second, third) for item in page["items"]]
        self.assertEqual(121, len(set(ids)))
        self.assertEqual(121, len(ids))
        self.assertIsNone(third["next_cursor"])

    async def test_entity_hash_cache_miss_resolves_both_historical_id_shapes_from_dialogs(self):
        self.client.dialogs = [dialog(group(1358434454, channel=True)), dialog(group(1890322660, channel=True))]
        await self.collector.connect()
        for identifier in ("-1001358434454", "-1001890322660"):
            entity = await self.collector._entity(identifier)
            self.assertEqual(identifier, str(utils.get_peer_id(entity)))
        self.assertEqual([], self.client.rpc_calls)

    async def test_public_username_resolves_to_stable_group_id(self):
        entity = group(32, channel=True, username="ExpertNews")
        self.client.dialogs = [dialog(entity)]
        result = await self.collector.control("/admins", {"chat_id": "@expertnews"})
        self.assertEqual("ready", result["state"], result)
        self.assertEqual("-1000000000032", result["chat_id"])

    async def test_missing_entity_does_not_claim_admin_list_is_empty(self):
        result = await self.collector.control("/admins", {"chat_id": "-1001890322660"})
        self.assertEqual("unavailable", result["state"])
        self.assertEqual("account_has_not_joined_or_entity_is_unavailable", result["reason"])
        self.assertFalse(result["manual_id_allowed"])

    async def test_group_ids_cannot_resolve_to_a_private_user_or_forbidden_channel(self):
        self.client.entities["-123"] = User(id=123, first_name="不是群")
        result = await self.collector.control("/admins", {"chat_id": "-123"})
        self.assertEqual("source_not_group_or_channel", result["reason"])
        self.client.entities["-123"] = ChannelForbidden(id=123, access_hash=1, title="不可访问")
        result = await self.collector.control("/admins", {"chat_id": "-123"})
        self.assertEqual("source_not_accessible", result["reason"])

    async def test_left_and_migrated_groups_have_distinct_failure_states(self):
        entity = group(12)
        entity.left = True
        self.use_group(entity)
        result = await self.collector.control("/admins", {"chat_id": "-12"})
        self.assertEqual("account_has_not_joined", result["reason"])
        entity.left = False
        entity.migrated_to = InputChannel(channel_id=99, access_hash=1)
        result = await self.collector.control("/admins", {"chat_id": "-12"})
        self.assertEqual("source_migrated", result["reason"])
        self.assertEqual("-1000000000099", result["resolved_chat_id"])

    async def test_basic_group_returns_only_actual_admins_and_creator(self):
        chat_id = self.use_group(group(12))
        self.client.users = [User(id=7, first_name="同名"), User(id=8, first_name="同名"), User(id=9, first_name="普通成员")]
        self.client.roles = [ChatParticipantCreator(7), ChatParticipantAdmin(8, 7, NOW), ChatParticipant(9, 7, NOW)]
        result = await self.collector.control("/admins", {"chat_id": chat_id})
        self.assertEqual(["7", "8"], [item["sender_id"] for item in result["items"]])
        self.assertEqual(["owner", "administrator"], [item["admin_kind"] for item in result["items"]])
        self.assertTrue(all(not item["has_avatar"] for item in result["items"]))
        self.assertEqual(1, len(self.client.rpc_calls))
        self.assertIsInstance(self.client.rpc_calls[0], GetFullChatRequest)
        self.assertTrue(result["anonymous_admins_may_be_unlisted"])

    async def test_channel_admin_pages_reach_every_stable_user_id(self):
        chat_id = self.use_group(group(99, channel=True))
        self.client.users = [User(id=index, first_name="同名") for index in range(1, 124)]
        self.client.roles = [ChannelParticipantCreator(1, ChatAdminRights())] + [ChannelParticipantAdmin(user.id, 1, NOW, ChatAdminRights()) for user in self.client.users[1:]]
        ids, cursor = [], None
        for _ in range(3):
            page = await self.collector.control("/admins", {"chat_id": chat_id, "cursor": cursor})
            self.assertEqual("ready", page["state"], page)
            ids.extend(item["sender_id"] for item in page["items"])
            cursor = page["next_cursor"]
        self.assertEqual(123, len(set(ids)))
        self.assertEqual([0, 50, 100], [request.offset for request in self.client.rpc_calls])
        self.assertIsNone(cursor)

    async def test_restricted_member_enumeration_allows_manual_trust_without_claiming_admin_identity(self):
        chat_id = self.use_group(group(12))
        self.client.forbidden = True
        result = await self.collector.control("/admins", {"chat_id": chat_id})
        self.assertEqual("member_enumeration_restricted", result["reason"])
        self.assertTrue(result["manual_id_allowed"])
        self.assertEqual("unavailable", result["state"])

    async def test_access_and_rate_failures_are_classified_without_exception_details(self):
        chat_id = self.use_group(group(12))
        for error, reason in ((ChatAdminRequiredError(None), "member_enumeration_restricted"),
                              (ChannelPrivateError(None), "source_not_accessible"),
                              (UserNotParticipantError(None), "account_has_not_joined"),
                              (ConnectionError("must-not-disclose"), "network_unavailable")):
            self.client.error = error
            result = await self.collector.control("/admins", {"chat_id": chat_id})
            self.assertEqual(reason, result["reason"])
            self.assertNotIn("must-not-disclose", str(result))
        self.client.error = FloodWaitError(None, capture=42)
        result = await self.collector.control("/admins", {"chat_id": chat_id})
        self.assertEqual("flood_wait", result["state"])
        self.assertEqual(42, result["retry_after"])

    async def test_invalid_identifiers_cursors_and_search_are_rejected(self):
        for identifier in ("1358434454", "群组名字", "-0", "-9223372036854775809", "@a", "https://t.me/channel"):
            result = await self.collector.control("/admins", {"chat_id": identifier})
            self.assertEqual("invalid_source_identifier", result["reason"], result)
        for cursor in ("-1", "nan", "1000001", "１２"):
            result = await self.collector.control("/dialogs", {"cursor": cursor})
            self.assertEqual("invalid_pagination_cursor", result["reason"])
        result = await self.collector.control("/dialogs", {"q": "x\nsecret"})
        self.assertEqual("invalid_search_query", result["reason"])

    async def test_incomplete_admin_rpc_is_not_reported_as_finished_page(self):
        chat_id = self.use_group(group(12))
        self.client.roles = [ChatParticipantCreator(7)]
        result = await self.collector.control("/admins", {"chat_id": chat_id})
        self.assertEqual("incomplete_admin_page", result["reason"])
        self.assertEqual("unavailable", result["state"])

    def seed_many_channel_admins(self):
        chat_id = self.use_group(group(99, channel=True))
        self.client.users = [User(id=index, first_name=f"专家{index}", username=f"expert{index}") for index in range(1, 406)]
        self.client.roles = [ChannelParticipantAdmin(user.id, 1, NOW, ChatAdminRights()) for user in self.client.users]
        return chat_id

    async def test_admin_search_reads_beyond_first_page_and_honors_server_short_pages(self):
        chat_id = self.seed_many_channel_admins()
        self.client.page_cap = 100
        for query in ("专家405", "405", "@Expert405"):
            self.client.rpc_calls.clear()
            result = await self.collector.control("/admins", {"chat_id": chat_id, "q": query})
            self.assertEqual("ready", result["state"], result)
            self.assertEqual(["405"], [item["sender_id"] for item in result["items"]])
            self.assertEqual([0, 100, 200, 300, 400], [request.offset for request in self.client.rpc_calls])

    async def test_admin_search_pages_filtered_results_and_empty_search_is_explicit(self):
        chat_id = self.seed_many_channel_admins()
        first = await self.collector.control("/admins", {"chat_id": chat_id, "q": "专家"})
        second = await self.collector.control("/admins", {"chat_id": chat_id, "q": "专家", "cursor": first["next_cursor"]})
        self.assertEqual([str(index) for index in range(1, 51)], [item["sender_id"] for item in first["items"]])
        self.assertEqual([str(index) for index in range(51, 101)], [item["sender_id"] for item in second["items"]])
        empty = await self.collector.control("/admins", {"chat_id": chat_id, "q": "未找到的作者"})
        self.assertEqual([], empty["items"])
        self.assertIsNone(empty["next_cursor"])
        self.assertEqual("未找到的作者", empty["q"])

    async def test_admin_search_partial_failure_or_stalled_pagination_never_returns_partial_success(self):
        chat_id = self.seed_many_channel_admins()
        self.client.fail_at = 200
        failed = await self.collector.control("/admins", {"chat_id": chat_id, "q": "专家"})
        self.assertEqual("flood_wait", failed["state"])
        self.assertEqual([], failed["items"])
        self.client.fail_at = None
        self.client.repeat_page = True
        stalled = await self.collector.control("/admins", {"chat_id": chat_id, "q": "专家"})
        self.assertEqual("directory_pagination_stalled", stalled["reason"])
        self.assertEqual("unavailable", stalled["state"])
