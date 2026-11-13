"""Explicit buy intents over task-owned databases; no funded/provider requests."""
import asyncio
from decimal import Decimal
import unittest
import uuid
import asyncpg
import httpx
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
    native = support.AccountStateTests.native

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

    async def test_older_manual_intent_rechecks_and_submits_current_provider_price(self):
        from fastapi import HTTPException
        from services.api.app import orders
        from services.executor.account_protocol import AccountProtocolClient, BET_PATH, LATEST_MARKET_PATH
        from services.executor.worker import Executor
        signal, quote = await self.configured()
        await self.pool.execute("UPDATE v2_expert_signals SET created_at=now()-interval '1 hour' WHERE id=$1", signal)
        await self.pool.execute("UPDATE v2_expert_messages SET posted_at=now()-interval '1 hour'")
        await self.pool.execute("UPDATE v2_current_quotes SET price=1.50,source_updated_at=now()-interval '10 minutes' WHERE id=$1", quote)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                with self.assertRaises(HTTPException):
                    await orders.eligible_signal(conn, signal)
        response = await self.buy(signal)
        self.assertEqual(202, response.status_code, response.text)
        work = (await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        self.assertEqual('order', work['kind'])

        def current_provider(request):
            result = self.native(request)
            if request.url.path == LATEST_MARKET_PATH:
                value = result.json()
                value['data'][0]['marketOddsList'][0]['oddsValue'] = '235000'
                return httpx.Response(200, json=value)
            if request.url.path == BET_PATH:
                import json
                detail = json.loads(request.content)['seriesOrders'][0]['orderDetailList'][0]
                self.assertEqual(('2.35', '235000'), (detail['oddFinally'], detail['odds']))
            return result

        executor = Executor(self.worker_api, 'https://platform.invalid/internal/executor', live_orders_enabled=True,
            client_factory=lambda scope, session, **kwargs: AccountProtocolClient(
                scope, session, transport=httpx.MockTransport(current_provider), **kwargs))
        await executor.order_job(work)
        stored = await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1', uuid.UUID(work['order_id']))
        self.assertEqual((True, 'accepted', Decimal('2.35')), (stored['manual'], stored['state'], stored['price']))
        self.assertEqual(Decimal('1.95'), Decimal(stored['quote_snapshot']['expert_recommendation_price']))
        self.assertEqual(1, sum(request.url.path == BET_PATH for request in self.native_requests))

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

    async def copy_intent(self, signal, *, other_expert=False, **changes):
        row = dict(await self.pool.fetchrow('SELECT * FROM v2_expert_signals WHERE id=$1', signal))
        message = uuid.uuid4()
        if other_expert:
            row['expert_id'] = uuid.uuid4()
            await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,$2,'Other')",
                                    row['expert_id'], str(row['expert_id']))
            await self.pool.execute('INSERT INTO v2_source_experts(source_id,expert_id) SELECT source_id,$1 FROM v2_expert_messages WHERE id=$2', row['expert_id'], row['message_id'])
            await self.pool.execute('UPDATE v2_user_bet_settings SET allowed_experts=array_append(allowed_experts,$1)', row['expert_id'])
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) "
                                "SELECT $1,source_id,$2,$3,'repeat',now() FROM v2_expert_messages WHERE id=$4",
                                message, row['expert_id'], str(message), row['message_id'])
        row.update(id=uuid.uuid4(), message_id=message, **changes)
        keys = ('id','message_id','expert_id','fixture_id','phase','market_id','market_name','market_kind','period',
                'line','selection','recommendation_price','quote_time','confidence','score_basis','score_baseline')
        await self.pool.execute('INSERT INTO v2_expert_signals('+','.join(keys)+') VALUES('+','.join(f'${i+1}' for i in range(len(keys)))+')',
                                *(row[key] for key in keys))
        return row['id']

    async def test_concurrent_different_signal_and_expert_reserve_one_automatic_order(self):
        signal, _ = await self.configured()
        second = await self.copy_intent(signal, other_expert=True)
        orders = await asyncio.gather(self.reserve(signal), self.reserve(second))
        self.assertEqual(1, len({o['id'] for o in orders}))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))
        self.assertEqual(Decimal('2'), await self.pool.fetchval('SELECT sum(reserved_stake) FROM v2_bet_orders'))
        for identity in (signal, second):
            response = await self.buy(identity)
            self.assertEqual(202, response.status_code, response.text)
        self.assertEqual(3, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_accepted_unknown_and_settled_orders_block_reposted_automatic_intent(self):
        signal, _ = await self.configured()
        first = await self.reserve(signal)
        second = await self.copy_intent(signal)
        for state in ('accepted','unknown','settled','cancelled'):
            await self.pool.execute('UPDATE v2_bet_orders SET state=$2 WHERE id=$1', first['id'], state)
            self.assertEqual(first['id'], (await self.reserve(second))['id'])
            response = await self.worker_api.post('/internal/executor/dispatch')
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(0, response.json()['reserved'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_changed_line_period_direction_and_relative_score_are_distinct_intents(self):
        from services.api.app.signal_identity import previous_intent_order
        signal, _ = await self.configured()
        await self.pool.execute("UPDATE v2_expert_signals SET score_basis='remaining_period',score_baseline='{\"home\":1,\"away\":0}'::jsonb WHERE id=$1", signal)
        first = await self.reserve(signal)
        for change in ({'line':Decimal('3.5')}, {'period':'first_half'}, {'selection':'under'},
                       {'score_baseline':{'home':2,'away':0}}):
            other = await self.copy_intent(signal, **change)
            async with self.pool.acquire() as conn:
                self.assertIsNone(await previous_intent_order(conn, self.user['id'], first['account_id'], other))

    async def test_unsent_legacy_duplicate_fails_execution_recheck(self):
        from services.api.app.orders import recheck_order
        from fastapi import HTTPException
        signal, _ = await self.configured()
        first = await self.reserve(signal)
        second = await self.copy_intent(signal)
        duplicate = await self.pool.fetchrow("INSERT INTO v2_bet_orders(id,user_id,signal_id,idempotency_key,state,stake,price,account_id,binding_revision,config_revision,policy_revision,provider_identity_hash,reserved_stake,quote_snapshot,manual) "
            "SELECT $1,user_id,$2,$3,'pending',stake,price,account_id,binding_revision,config_revision,policy_revision,provider_identity_hash,reserved_stake,quote_snapshot,false FROM v2_bet_orders WHERE id=$4 RETURNING *",
            uuid.uuid4(), second, str(uuid.uuid4()), first['id'])
        async with self.pool.acquire() as conn, conn.transaction():
            await recheck_order(conn, first, self.settings)
            with self.assertRaisesRegex(HTTPException, '重复自动投注'):
                await recheck_order(conn, duplicate, self.settings)

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
