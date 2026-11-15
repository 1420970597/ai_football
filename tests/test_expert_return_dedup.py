"""Returns count the first expert intent, never the best duplicate's price."""
import os
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from app.expert_profitability import profitability_rows, profitable
from app.expert_statistics import StatisticsScope, signal_scope, summaries, trend
from app.expert_statistics_reset import apply_reset, preview_reset, revoke_reset
from app.routes import public
import test_result_ledger as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires task-owned isolated PostgreSQL')
class ExpertReturnDedupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ResultLedgerTests.asyncSetUp
    asyncTearDown = support.ResultLedgerTests.asyncTearDown
    result = support.ResultLedgerTests.result
    submit = support.ResultLedgerTests.submit

    async def pick(self, *, price='2', source=None, expert=None, **kwargs):
        message = uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,$4,'same intent',now())",
                                message, source or self.source, expert or self.expert, str(message))
        old_message, old_expert = self.message, self.expert
        self.message, self.expert = message, expert or self.expert
        try:
            identity = await support.ResultLedgerTests.signal(self, **kwargs)
        finally:
            self.message, self.expert = old_message, old_expert
        await self.pool.execute('UPDATE v2_expert_signals SET recommendation_price=$2 WHERE id=$1', identity, Decimal(price))
        await self.pool.execute('UPDATE v2_quote_evidence SET price=$2 WHERE reference_id=$1', identity, Decimal(price))
        return identity

    def scope(self, **kwargs):
        return StatisticsScope(self.now-timedelta(days=30), self.now+timedelta(days=1), **kwargs)

    async def ids(self, scope=None):
        prefix, args = signal_scope(scope or self.scope(), classify=False)
        return {row['id'] for row in await self.pool.fetch(prefix+'SELECT id FROM scoped', *args)}

    async def summary(self, scope=None):
        scope = scope or self.scope()
        return (await summaries(self.pool, scope.start, scope.end, scope=scope, expert_id=self.expert))[0]

    async def test_unmarked_repeat_counts_once_with_first_price_everywhere(self):
        first = await self.pick(price='1.8', line='2.5')
        await self.pool.execute("UPDATE v2_expert_signals SET created_at=now()-interval '2 minutes' WHERE id=$1", first)
        for price in ('2.4', '9'):
            await self.pick(price=price, line='2.5')
        self.assertEqual(200, (await self.submit(self.result(home=3, away=0))).status_code)
        row = await self.summary()
        self.assertEqual((1, 1, 1), (row['total_signals'], row['settled_signals'], row['win']))
        self.assertAlmostEqual(.8, row['unit_net'])
        self.assertAlmostEqual(.8, row['average_unit_net'])
        self.assertAlmostEqual(.8, row['unit_roi'])
        self.assertEqual(1, row['accuracy'])
        series = await trend(self.pool, self.expert, self.scope())
        self.assertEqual(Decimal('.8'), series[-1]['cumulative_unit_net'])
        self.assertEqual(1, series[-1]['cumulative_settled'])
        profitable = (await profitability_rows(self.pool, expert_id=self.expert, at=self.scope().end))[0]
        self.assertEqual((1, Decimal('.8')), (profitable['effective_settled_signals'], profitable['unit_net']))
        self.assertEqual(3, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(3, await self.pool.fetchval('SELECT count(*) FROM v2_signal_settlements'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_dedup_precedes_window_source_and_statistics_reset(self):
        first = await self.pick(line='2.5')
        await self.pool.execute("UPDATE v2_expert_signals SET created_at=now()-interval '40 days' WHERE id=$1", first)
        source = uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,$2,'other source')", source, str(source))
        duplicate = await self.pick(line='2.5', source=source)
        self.assertEqual(set(), await self.ids())
        self.assertEqual(set(), await self.ids(self.scope(source_id=source)))
        self.assertEqual(200, (await self.submit(self.result(home=0, away=0))).status_code)
        plan = await preview_reset(self.pool)
        self.assertEqual([str(first)], [s['signal_id'] for s in plan['signals']])
        from datetime import datetime
        reset = await apply_reset(self.pool, idempotency_key='dedup-cycle', expected_fingerprint=plan['fingerprint'],
                                  as_of=datetime.fromisoformat(plan['as_of']), reason='isolated canonical reset')
        self.assertEqual(set(), await self.ids())
        self.assertEqual(0, (await self.summary())['total_signals'])
        await revoke_reset(self.pool, reset_id=uuid.UUID(reset['reset_id']), reason='isolated restore')
        self.assertEqual({first}, await self.ids())
        self.assertNotIn(duplicate, await self.ids())

    async def test_distinct_experts_lines_directions_periods_phases_and_live_baselines_survive(self):
        expected = {await self.pick(line='2.5')}
        await self.pick(line='2.5')
        for options in ({'line':'3'}, {'line':'2.5','selection':'under'},
                        {'line':'2.5','period':'first_half'}, {'line':'2.5','phase':'live'}):
            expected.add(await self.pick(**options))
        expert = uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,$2,'same nickname')", expert, str(expert))
        expected.add(await self.pick(expert=expert, line='2.5'))
        changed = await self.pick(line='2.5', phase='live')
        await self.pool.execute('UPDATE v2_expert_signals SET score_baseline=$2 WHERE id=$1', changed, {'home':1,'away':0})
        expected.add(changed)
        self.assertEqual(expected, await self.ids())

    async def test_missing_original_evidence_does_not_borrow_repeat_price_or_profit(self):
        first = await self.pick(line='2.5', evidence=False)
        await self.pool.execute("UPDATE v2_expert_signals SET created_at=now()-interval '1 minute' WHERE id=$1", first)
        await self.pick(line='2.5', price='9')
        row = await self.summary()
        self.assertEqual((1,1,0), (row['total_signals'],row['excluded_signals'],row['eligible_signals']))
        self.assertEqual(0, row['unit_net'])

    async def test_corrected_invalid_intent_does_not_suppress_valid_repeat(self):
        invalid = await self.pick(line='2.5')
        await self.pool.execute("UPDATE v2_expert_signals SET withdrawn_at=now(),withdrawal_reason='review_source_line_intent_conflict' WHERE id=$1", invalid)
        valid = await self.pick(line='2.5')
        self.assertIn(valid, await self.ids())
        self.assertEqual(1, (await self.summary())['eligible_signals'])

    async def test_signal_feed_deduplicates_before_limit_and_cursor(self):
        first = await self.pick(line='2.5')
        for _ in range(7):
            await self.pick(line='2.5')
        other = await self.pick(line='3.5')
        with patch.object(public, '_has_entitlement', AsyncMock(return_value=True)), \
                patch.object(public, 'get_identity', AsyncMock(return_value={'role':'user'})):
            response = await self.client.get('/api/v1/public/signals', params={'page_size':1})
            self.assertEqual(200,response.status_code,response.text)
            a=response.json()
            self.assertEqual([str(other)],[s['id'] for s in a['items']])
            b=(await self.client.get('/api/v1/public/signals',params={'page_size':1,'cursor':a['next_cursor']})).json()
            self.assertEqual([str(first)],[s['id'] for s in b['items']])
            self.assertIsNone(b['next_cursor'])

    async def test_tied_timestamps_use_id_and_full_period_scores_do_not_multiply_returns(self):
        a = await self.pick(phase='live',line='2.5')
        b = await self.pick(phase='live',line='2.5')
        await self.pool.execute("UPDATE v2_expert_signals SET created_at=$1,score_basis='full_period'", self.now)
        await self.pool.execute('UPDATE v2_expert_signals SET score_baseline=$2 WHERE id=$1', b, {'home':2,'away':0})
        self.assertEqual({min(a,b)}, await self.ids())

    async def test_duplicate_wins_cannot_create_false_profitable_expert(self):
        first = await self.pick(line='2.5',price='1.6')
        await self.pool.execute("UPDATE v2_expert_signals SET created_at=now()-interval '1 minute' WHERE id=$1",first)
        for _ in range(2):
            await self.pick(line='2.5',price='1.6')
        await self.pick(line='3.5')
        self.assertEqual(200,(await self.submit(self.result(home=3,away=0))).status_code)
        row=(await profitability_rows(self.pool,expert_id=self.expert))[0]
        self.assertEqual((2,Decimal('-.4')),(row['effective_settled_signals'],row['unit_net']))
        self.assertFalse(profitable(row))
