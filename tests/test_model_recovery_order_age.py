"""Old Telegram posts remain reviewable without becoming new money candidates."""
import unittest

from fastapi import HTTPException

import test_account_state as fixture


@unittest.skipUnless(fixture.DSN, 'requires task-owned temporary Docker database')
class ModelRecoveryOrderAgeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.AccountStateTests.asyncSetUp
    asyncTearDown = fixture.AccountStateTests.asyncTearDown
    recent_session = fixture.AccountStateTests.recent_session
    bind = fixture.AccountStateTests.bind
    healthy = fixture.AccountStateTests.healthy
    signal = fixture.AccountStateTests.signal
    enable = fixture.AccountStateTests.enable
    reserve = fixture.AccountStateTests.reserve

    async def test_old_post_and_new_edit_cannot_reserve_newly_generated_signal(self):
        await self.healthy()
        for revision in (1, 2):
            with self.subTest(revision=revision):
                signal, expert, message, _ = await self.signal()
                await self.enable([expert])
                await self.pool.execute("UPDATE v2_expert_messages SET posted_at=now()-interval '8 minutes',revision=$2 WHERE id=$1", message, revision)
                before = await self.pool.fetchrow('SELECT * FROM v2_expert_signals WHERE id=$1', signal)
                with self.assertRaises(HTTPException) as caught:
                    await self.reserve(signal)
                self.assertEqual(409, caught.exception.status_code)
                after = await self.pool.fetchrow('SELECT * FROM v2_expert_signals WHERE id=$1', signal)
                self.assertEqual(dict(before), dict(after))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_fresh_original_post_keeps_existing_personal_permission(self):
        await self.healthy()
        signal, expert, message, _ = await self.signal()
        await self.enable([expert])
        await self.pool.execute("UPDATE v2_expert_messages SET posted_at=now()-interval '4 minutes 30 seconds' WHERE id=$1", message)
        policy = await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1', self.user['id'])
        order = await self.reserve(signal)
        self.assertEqual(signal, order['signal_id'])
        self.assertEqual(2, order['reserved_stake'])
        self.assertEqual(dict(policy), dict(await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1', self.user['id'])))
        self.assertEqual([], self.native_requests)

    async def test_source_post_older_than_five_minutes_is_not_retimed_by_signal_creation(self):
        await self.healthy()
        signal, expert, message, _ = await self.signal()
        await self.enable([expert])
        await self.pool.execute("UPDATE v2_expert_messages SET posted_at=now()-interval '5 minutes 1 second' WHERE id=$1", message)
        with self.assertRaises(HTTPException) as caught:
            await self.reserve(signal)
        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_obviously_future_source_post_cannot_reserve(self):
        await self.healthy()
        signal, expert, message, _ = await self.signal()
        await self.enable([expert])
        await self.pool.execute("UPDATE v2_expert_messages SET posted_at=now()+interval '1 minute' WHERE id=$1", message)
        with self.assertRaises(HTTPException) as caught:
            await self.reserve(signal)
        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_pending_order_loses_permission_when_original_post_ages_out(self):
        await self.healthy()
        signal, expert, message, _ = await self.signal()
        await self.enable([expert])
        order = await self.reserve(signal)
        await self.pool.execute("UPDATE v2_expert_messages SET posted_at=now()-interval '8 minutes' WHERE id=$1", message)
        response = await self.worker_api.post('/internal/executor/claim?live_capable=true')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('idle', response.json()['kind'])
        stored = await self.pool.fetchrow('SELECT state,reserved_stake,attempt_id FROM v2_bet_orders WHERE id=$1', order['id'])
        self.assertEqual(('cancelled', 0, None), tuple(stored))
        self.assertEqual([], self.native_requests)

    async def test_old_recovered_posts_do_not_fill_dispatch_limit_ahead_of_new_post(self):
        await self.healthy()
        old, experts = [], []
        for _ in range(31):
            signal, expert, message, _ = await self.signal()
            old.append(signal)
            experts.append(expert)
            await self.pool.execute("UPDATE v2_expert_messages SET posted_at=now()-interval '8 minutes' WHERE id=$1", message)
            await self.pool.execute("UPDATE v2_expert_signals SET created_at=now()-interval '1 minute' WHERE id=$1", signal)
        fresh, expert, _, _ = await self.signal()
        experts.append(expert)
        await self.enable(experts)
        response = await self.worker_api.post('/internal/executor/dispatch')
        self.assertEqual(200, response.status_code, response.text)
        orders = await self.pool.fetch('SELECT signal_id,reserved_stake FROM v2_bet_orders')
        self.assertEqual([(fresh, 2)], [tuple(row) for row in orders])
        self.assertEqual(32, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual([], self.native_requests)
