"""Queue coverage with isolated accounts; never submit provider orders."""
import unittest

import test_account_state as fixture


@unittest.skipUnless(fixture.DSN, 'requires task-owned temporary Docker database')
class AutoDispatchCoverageTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.AccountStateTests.asyncSetUp
    asyncTearDown = fixture.AccountStateTests.asyncTearDown
    recent_session = fixture.AccountStateTests.recent_session
    bind = fixture.AccountStateTests.bind
    healthy = fixture.AccountStateTests.healthy
    signal = fixture.AccountStateTests.signal
    enable = fixture.AccountStateTests.enable

    async def test_rejected_first_thirty_do_not_starve_later_experts(self):
        await self.healthy()
        invalid, experts = [], []
        for index in range(32):
            signal, expert, message, quote = await self.signal()
            invalid.append(signal)
            experts.append(expert)
            if index % 4 == 0:
                await self.pool.execute("UPDATE v2_expert_sources SET enabled=false WHERE id=(SELECT source_id FROM v2_expert_messages WHERE id=$1)", message)
            elif index % 4 == 1:
                await self.pool.execute("UPDATE v2_expert_messages SET deleted_at=now() WHERE id=$1", message)
            elif index % 4 == 2:
                await self.pool.execute("UPDATE v2_expert_messages SET media_state='pending' WHERE id=$1", message)
            else:
                await self.pool.execute("UPDATE v2_current_quotes SET play_id=NULL WHERE id=$1", quote)
        fresh, expert, _, _ = await self.signal()
        await self.enable(experts+[expert])
        response = await self.worker_api.post('/internal/executor/dispatch')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, response.json()['reserved'])
        self.assertEqual([fresh], [row['signal_id'] for row in await self.pool.fetch('SELECT signal_id FROM v2_bet_orders')])
        self.assertEqual(0, (await self.worker_api.post('/internal/executor/dispatch')).json()['reserved'])
        self.assertEqual([], self.native_requests)

    async def test_more_than_thirty_fresh_experts_are_all_considered_in_one_dispatch(self):
        await self.healthy()
        signals, experts = [], []
        for _ in range(35):
            signal, expert, _, _ = await self.signal()
            signals.append(signal)
            experts.append(expert)
        await self.enable(experts)
        await self.pool.execute('UPDATE v2_user_bet_settings SET daily_limit=100,open_exposure_limit=100')
        result = await self.worker_api.post('/internal/executor/dispatch')
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual(35, result.json()['reserved'])
        self.assertEqual(set(signals), {row['signal_id'] for row in await self.pool.fetch('SELECT signal_id FROM v2_bet_orders')})
        self.assertEqual(0, (await self.worker_api.post('/internal/executor/dispatch')).json()['reserved'])
        self.assertEqual([], self.native_requests)
