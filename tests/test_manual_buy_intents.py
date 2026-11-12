"""Explicit buy intents over task-owned databases; no funded/provider requests."""
import asyncio
from decimal import Decimal
import unittest
import uuid
import asyncpg
import test_account_state as support


@unittest.skipUnless(support.DSN, 'requires task-owned temporary Docker database')
class ManualBuyIntentTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.AccountStateTests.asyncSetUp
    asyncTearDown = support.AccountStateTests.asyncTearDown
    recent_session = support.AccountStateTests.recent_session
    healthy = support.AccountStateTests.healthy
    bind = support.AccountStateTests.bind
    signal = support.AccountStateTests.signal
    enable = support.AccountStateTests.enable
    reserve = support.AccountStateTests.reserve

    async def configured(self, limit='10'):
        await self.healthy()
        signal, expert, message, quote = await self.signal()
        await self.enable([expert])
        response = await self.api.put('/api/v1/accounts/leyu/manual-betting', json={
            'stake_per_order':'2','daily_limit':limit,'open_exposure_limit':limit,'daily_timezone':'Asia/Shanghai'})
        self.assertEqual(200, response.status_code, response.text)
        return signal, quote

    async def buy(self, signal, request=None):
        return await self.api.post('/api/v1/accounts/leyu/manual-betting/orders', json={
            'signal_id':str(signal), 'request_id':str(request or uuid.uuid4())})

    async def test_accepted_history_does_not_replace_new_confirmation_and_current_price(self):
        signal, quote = await self.configured()
        first_request = uuid.uuid4()
        first = await self.buy(signal, first_request)
        self.assertEqual(202, first.status_code, first.text)
        old = uuid.UUID(first.json()['order']['id'])
        await self.pool.execute("UPDATE v2_bet_orders SET state='accepted',reserved_stake=0 WHERE id=$1", old)
        before = await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1', old)
        await self.pool.execute('UPDATE v2_current_quotes SET price=2.35 WHERE id=$1', quote)
        new = await self.buy(signal)
        self.assertEqual(202, new.status_code, new.text)
        self.assertNotEqual(str(old), new.json()['order']['id'])
        self.assertEqual(('pending',False,Decimal('2.35')),
                         (new.json()['order']['state'],new.json()['replayed'],Decimal(new.json()['order']['price'])))
        replay = await self.buy(signal, first_request)
        self.assertEqual((str(old),'accepted',True),
            (replay.json()['order']['id'],replay.json()['order']['state'],replay.json()['replayed']))
        self.assertEqual(before, await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1', old))
        self.assertEqual(2, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_same_request_concurrent_replay_reserves_once(self):
        signal, _ = await self.configured()
        request = uuid.uuid4()
        responses = await asyncio.gather(self.buy(signal, request), self.buy(signal, request))
        self.assertEqual([202,202], [r.status_code for r in responses])
        self.assertEqual(1, len({r.json()['order']['id'] for r in responses}))
        self.assertEqual({True,False}, {r.json()['replayed'] for r in responses})
        self.assertEqual(Decimal('2'), await self.pool.fetchval('SELECT sum(reserved_stake) FROM v2_bet_orders'))

    async def test_separate_confirmations_serialize_and_keep_daily_exposure_limits(self):
        signal, _ = await self.configured(limit='4')
        responses = await asyncio.gather(self.buy(signal), self.buy(signal), self.buy(signal))
        self.assertEqual([202,202,409], sorted(r.status_code for r in responses))
        self.assertEqual(2, len({r.json()['order']['id'] for r in responses if r.status_code==202}))
        self.assertEqual(Decimal('4'), await self.pool.fetchval('SELECT sum(reserved_stake) FROM v2_bet_orders'))

    async def test_request_id_cannot_be_reused_for_another_signal_or_binding(self):
        signal, _ = await self.configured()
        request = uuid.uuid4()
        self.assertEqual(202, (await self.buy(signal, request)).status_code)
        second, *_ = await self.signal()
        self.assertEqual(409, (await self.buy(second, request)).status_code)
        await self.pool.execute('UPDATE v2_leyu_accounts SET binding_revision=binding_revision+1')
        self.assertEqual(409, (await self.buy(signal, request)).status_code)
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_missing_request_and_automatic_duplicates_remain_safe(self):
        signal, _ = await self.configured()
        missing = await self.api.post('/api/v1/accounts/leyu/manual-betting/orders', json={'signal_id':str(signal)})
        self.assertEqual(422, missing.status_code)
        first, repeated = await self.reserve(signal), await self.reserve(signal)
        self.assertEqual(first['id'], repeated['id'])
        self.assertFalse(first['manual'])
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.pool.execute("INSERT INTO v2_bet_orders(id,user_id,signal_id,idempotency_key,state,stake,price,account_id,manual) "
                "SELECT $1,user_id,signal_id,$2,'pending',stake,price,account_id,FALSE FROM v2_bet_orders WHERE id=$3",
                uuid.uuid4(), str(uuid.uuid4()), first['id'])
        manual = await self.buy(signal)
        self.assertEqual(202, manual.status_code, manual.text)
        self.assertNotEqual(str(first['id']), manual.json()['order']['id'])

    async def test_upgrade_preserves_existing_manual_order_and_is_repeatable(self):
        from services.api.app.database import apply_migrations, check_schema
        signal, _ = await self.configured()
        first = (await self.buy(signal)).json()['order']
        async with self.pool.acquire() as conn:
            before = await conn.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1', uuid.UUID(first['id']))
            async with conn.transaction():
                await conn.execute('DROP INDEX v2_order_user_signal_unique')
                await conn.execute('CREATE UNIQUE INDEX v2_order_user_signal_unique ON v2_bet_orders(user_id,signal_id) WHERE account_id IS NOT NULL')
                await conn.execute("DELETE FROM v2_schema_migrations WHERE version='038_manual_buy_intents.sql'")
            self.assertEqual(['038_manual_buy_intents.sql'], await apply_migrations(conn))
            self.assertEqual([], await apply_migrations(conn))
            await check_schema(conn)
            self.assertEqual(before, await conn.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1', uuid.UUID(first['id'])))
        second = await self.buy(signal)
        self.assertEqual(202, second.status_code, second.text)
        self.assertNotEqual(first['id'], second.json()['order']['id'])
