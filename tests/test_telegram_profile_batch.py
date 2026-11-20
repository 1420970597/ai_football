"""Real PG nickname identity/history and fixed query budget contracts."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
import uuid

from app.telegram_profiles import profile_updates, sync_trusted_profiles
import test_telegram_api as legacy


class ProfileValidationTests(unittest.TestCase):
    def test_identical_duplicates_collapse_and_conflicts_or_control_text_are_rejected(self):
        self.assertEqual([{'sender_id': '7', 'nickname': '新昵称'}], profile_updates([
            SimpleNamespace(sender_id='7', nickname=' 新昵称 '),
            SimpleNamespace(sender_id='7', nickname='新昵称')]))
        for value in ('另一昵称', ' \n ', '不合法\n昵称'):
            with self.assertRaises(ValueError):
                profile_updates([SimpleNamespace(sender_id='7', nickname='新昵称'),
                                 SimpleNamespace(sender_id='7', nickname=value)])


@unittest.skipUnless(legacy.TEST_URL, 'requires explicit temporary Telegram database')
class ProfileBatchTests(unittest.IsolatedAsyncioTestCase):
    # Inherit only setup/teardown; the existing API contract runs separately.
    asyncSetUp = legacy.TelegramApiTests.asyncSetUp
    asyncTearDown = legacy.TelegramApiTests.asyncTearDown

    async def test_set_based_update_keeps_identity_aliases_and_unchanged_timestamp(self):
        now = datetime.now(timezone.utc)
        other, disabled, unmapped = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await self.pool.executemany(
            'INSERT INTO v2_experts(id,platform_sender_id,display_nickname,enabled) VALUES($1,$2,$3,$4)',
            [(other, '8', '同名', True), (disabled, '9', '停用', False), (unmapped, '10', '未信任', True)])
        await self.pool.executemany('INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)',
                                   [(self.source_id, other), (self.source_id, disabled)])
        profiles = [{'sender_id': str(x), 'nickname': '同名'} for x in (7, 8, 9, 10, 11)]
        class Counted:
            def __init__(self, connection): self.connection,self.calls = connection,0
            async def fetchval(self,*args):
                self.calls += 1
                return await self.connection.fetchval(*args)
        async with self.pool.acquire() as connection, connection.transaction():
            counted = Counted(connection)
            self.assertEqual(1, await sync_trusted_profiles(counted, self.source_id, profiles, now))
            self.assertEqual(1, counted.calls)
        self.assertEqual(4, await self.pool.fetchval('SELECT count(*) FROM v2_experts'))
        self.assertEqual('停用', await self.pool.fetchval('SELECT display_nickname FROM v2_experts WHERE id=$1', disabled))
        self.assertEqual('未信任', await self.pool.fetchval('SELECT display_nickname FROM v2_experts WHERE id=$1', unmapped))
        aliases = await self.pool.fetch('SELECT nickname FROM v2_expert_nickname_history WHERE expert_id=$1', self.expert_id)
        self.assertEqual({'测试专家', '同名'}, {x['nickname'] for x in aliases})
        timestamp = await self.pool.fetchval('SELECT updated_at FROM v2_experts WHERE id=$1', self.expert_id)
        async with self.pool.acquire() as connection, connection.transaction():
            self.assertEqual(0, await sync_trusted_profiles(connection, self.source_id, profiles, now-timedelta(seconds=1)))
        self.assertEqual(timestamp, await self.pool.fetchval('SELECT updated_at FROM v2_experts WHERE id=$1', self.expert_id))
        self.assertEqual(now, await self.pool.fetchval('SELECT last_seen_at FROM v2_expert_nickname_history WHERE expert_id=$1 AND nickname=$2', self.expert_id, '同名'))

    async def test_conflicting_profile_request_rejected_without_partial_directory_write(self):
        body={'chat_id':'-1001','owner':str(self.owner),'observed_at':datetime.now(timezone.utc).isoformat(),
              'state':'ready','admins':[],'profiles':[{'sender_id':'7','nickname':'甲'},{'sender_id':'7','nickname':'乙'}]}
        response=await self.client.put('/internal/telegram/directory',json=body)
        self.assertEqual(422,response.status_code)
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_telegram_directories'))
        self.assertEqual('测试专家',await self.pool.fetchval('SELECT display_nickname FROM v2_experts WHERE id=$1',self.expert_id))

    async def test_failed_transaction_rolls_back_nickname_and_history(self):
        with self.assertRaises(RuntimeError):
            async with self.pool.acquire() as connection, connection.transaction():
                await sync_trusted_profiles(connection,self.source_id,[{'sender_id':'7','nickname':'不可提交'}],datetime.now(timezone.utc))
                raise RuntimeError('synthetic later directory failure')
        self.assertEqual('测试专家',await self.pool.fetchval('SELECT display_nickname FROM v2_experts WHERE id=$1',self.expert_id))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_nickname_history'))

    async def test_other_group_and_disabled_mapping_cannot_rename_trusted_identity(self):
        other_source=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'-1002','独立群')",other_source)
        now=datetime.now(timezone.utc)
        profiles=[{'sender_id':'7','nickname':'不应变更'}]
        async with self.pool.acquire() as connection,connection.transaction():
            self.assertEqual(0,await sync_trusted_profiles(connection,other_source,profiles,now))
        await self.pool.execute('UPDATE v2_source_experts SET enabled=false WHERE source_id=$1',self.source_id)
        async with self.pool.acquire() as connection,connection.transaction():
            self.assertEqual(0,await sync_trusted_profiles(connection,self.source_id,profiles,now))
        self.assertEqual('测试专家',await self.pool.fetchval('SELECT display_nickname FROM v2_experts WHERE id=$1',self.expert_id))
