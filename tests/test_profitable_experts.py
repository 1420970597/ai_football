"""Owner-enabled dynamic experts and reversible evidence-preserving resets, no funds."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import unittest
import uuid

from fastapi import HTTPException

from app.expert_profitability import profitability_rows, profitable, require_expert_permission
from app.expert_statistics import StatisticsScope, summaries, trend
from app.expert_statistics_reset import apply_reset, preview_reset, revoke_reset
from services.api.app import orders, result_ledger
from services.api.app.routes.results import ResultInput
import test_account_state as account_tests


class ProfitabilityDefinitionTests(unittest.TestCase):
    def test_strict_decimal_profit_and_nonzero_effective_sample(self):
        for net, count, expected in [('0.000001', 1, True), ('0', 2, False), ('-1', 2, False), ('9', 0, False), ('NaN', 1, False), ('Infinity', 1, False)]:
            self.assertEqual(expected, profitable({'unit_net': Decimal(net), 'effective_settled_signals': count}))
        self.assertFalse(profitable(None))


@unittest.skipUnless(os.environ.get('ACCOUNT_TEST_DATABASE_URL'), 'requires owned isolated Docker PostgreSQL')
class ProfitableExpertTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = account_tests.AccountStateTests.asyncSetUp
    asyncTearDown = account_tests.AccountStateTests.asyncTearDown
    recent_session = account_tests.AccountStateTests.recent_session
    bind = account_tests.AccountStateTests.bind
    healthy = account_tests.AccountStateTests.healthy
    signal = account_tests.AccountStateTests.signal
    enable = account_tests.AccountStateTests.enable
    reserve = account_tests.AccountStateTests.reserve

    async def history(self, expert, *, home=3, line='2.5', price='1.95', evidence=True, age_days=0):
        signal, _, message, _ = await self.signal()
        await self.pool.execute('UPDATE v2_expert_signals SET expert_id=$2,line=$3,recommendation_price=$4,score_basis=\'full_period\' WHERE id=$1', signal, expert, Decimal(line), Decimal(price))
        await self.pool.execute('UPDATE v2_expert_messages SET expert_id=$2 WHERE id=$1', message, expert)
        if evidence:
            await self.pool.execute(
                "INSERT INTO v2_quote_evidence(id,evidence_kind,reference_id,fixture_id,market_id,market_name,period,line,selection,price,source_updated_at,evidence,score_baseline) "
                "SELECT $1,'expert_signal',id,fixture_id,market_id,market_name,period,line,selection,recommendation_price,quote_time,"
                "jsonb_build_object('score_basis',score_basis),score_baseline FROM v2_expert_signals WHERE id=$2", uuid.uuid4(), signal)
        fixture = await self.pool.fetchval('SELECT fixture_id FROM v2_expert_signals WHERE id=$1', signal)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await result_ledger.ingest_result(conn, ResultInput(fixture_id=fixture, provider_reference='isolated-official',
                    status='confirmed', home=home, away=0, source_updated_at=datetime.now(timezone.utc), evidence={'source':'test-only'}))
        if age_days:
            await self.pool.execute("UPDATE v2_expert_signals SET settled_at=now()-$2::int*interval '1 day' WHERE id=$1", signal, age_days)
        return signal

    async def mode(self, value, experts=()):
        response = await self.api.put('/api/v1/accounts/leyu/auto-betting', json={
            'enabled': True, 'confirmation_phrase':'I_UNDERSTAND', 'expert_selection_mode':value,
            'allowed_experts':[str(x) for x in experts], 'stake_per_order':'2', 'max_per_order':'2',
            'daily_limit':'4', 'open_exposure_limit':'4', 'daily_timezone':'Asia/Shanghai'})
        return response

    async def row(self, expert):
        rows = await profitability_rows(self.pool, expert_id=expert)
        return rows[0] if rows else None

    async def test_manual_default_and_owner_modes_preserve_existing_limits(self):
        await self.healthy()
        _, expert, _, _ = await self.signal()
        await self.enable([expert])
        before = await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1', self.user['id'])
        self.assertEqual('manual', before['expert_selection_mode'])
        self.assertEqual(200, (await self.mode('profitable', [expert])).status_code)
        after = await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1', self.user['id'])
        for key in ('stake_per_order','max_per_order','daily_limit','open_exposure_limit','consent_account_id','consent_binding_revision','allowed_experts'):
            self.assertEqual(before[key], after[key], key)
        account = (await self.api.get('/api/v1/accounts/leyu')).json()
        self.assertEqual('profitable', account['betting']['expert_selection_mode'])
        self.assertEqual(30, account['profitability_definition']['days'])
        self.assertEqual(422, (await self.mode('invalid')).status_code)
        self.assertEqual(422, (await self.mode('manual')).status_code)
        self.assertEqual(200, (await self.mode('manual', [expert])).status_code)

    async def test_dynamic_follows_unselected_profitable_expert_but_not_no_evidence_or_zero(self):
        await self.healthy()
        pending, expert, _, _ = await self.signal()
        await self.enable([expert])
        self.assertEqual(200, (await self.mode('profitable')).status_code)
        self.assertEqual([], (await self.api.get('/api/v1/accounts/leyu')).json()['betting']['allowed_experts'])
        with self.assertRaises(HTTPException):
            await self.reserve(pending)
        await self.history(expert, evidence=False)
        self.assertFalse(profitable(await self.row(expert)))
        await self.history(expert, home=2, line='2')  # push has no effective settlement
        self.assertEqual(0, (await self.row(expert))['effective_settled_signals'])
        await self.history(expert)
        self.assertTrue(profitable(await self.row(expert)))
        dispatched = await self.worker_api.post('/internal/executor/dispatch')
        self.assertEqual(1, dispatched.json()['reserved'], dispatched.text)
        order = await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE signal_id=$1', pending)
        self.assertIsNotNone(order)
        self.assertEqual(Decimal('2'), order['reserved_stake'])

    async def test_profitable_to_loss_is_rechecked_before_claim_and_authorization(self):
        await self.healthy()
        pending, expert, _, _ = await self.signal()
        await self.enable([expert])
        await self.mode('profitable')
        await self.history(expert)
        order = await self.reserve(pending)
        await self.history(expert, home=0)
        self.assertFalse(profitable(await self.row(expert)))
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                with self.assertRaises(HTTPException):
                    await orders.recheck_order(conn, order, self.settings)
        work = (await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        self.assertEqual('idle', work['kind'])
        cancelled = await self.pool.fetchrow('SELECT state,reserved_stake FROM v2_bet_orders WHERE id=$1', order['id'])
        self.assertEqual(('cancelled', Decimal('0')), tuple(cancelled.values()))

    async def test_manual_does_not_expand_and_fixed_window_excludes_old_profit(self):
        _, expert, _, _ = await self.signal()
        _, other, _, _ = await self.signal()
        await self.history(other)
        with self.assertRaises(HTTPException):
            await require_expert_permission(self.pool, {'expert_selection_mode':'manual','allowed_experts':[expert]}, other)
        await self.history(expert, age_days=31)
        self.assertFalse(profitable(await self.row(expert)))

    async def test_profitable_mode_still_rejects_disabled_expert_source_and_trust(self):
        await self.healthy()
        signal, expert, message, _ = await self.signal()
        await self.enable([expert])
        await self.mode('profitable')
        await self.history(expert)
        source = await self.pool.fetchval('SELECT source_id FROM v2_expert_messages WHERE id=$1', message)
        switches = [('v2_experts', 'id', expert), ('v2_expert_sources', 'id', source), ('v2_source_experts', 'source_id', source)]
        for table, column, value in switches:
            with self.subTest(disabled=table):
                await self.pool.execute(f'UPDATE {table} SET enabled=FALSE WHERE {column}=$1', value)
                self.assertEqual(0, (await self.worker_api.post('/internal/executor/dispatch')).json()['reserved'])
                with self.assertRaises(HTTPException):
                    await self.reserve(signal)
                await self.pool.execute(f'UPDATE {table} SET enabled=TRUE WHERE {column}=$1', value)
        order = await self.reserve(signal)
        work = (await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        self.assertEqual('order', work['kind'])
        for table, column, value in switches:
            with self.subTest(send_disabled=table):
                await self.pool.execute(f'UPDATE {table} SET enabled=FALSE WHERE {column}=$1', value)
                response = await self.worker_api.post(f"/internal/executor/orders/{order['id']}/authorize", json={
                    'attempt_id':work['attempt_id'], 'prepared_digest':'0'*64, 'payload_json':'{}',
                    'quote_checked_at':datetime.now(timezone.utc).isoformat()})
                self.assertEqual(409, response.status_code, response.text)
                self.assertIn('推荐、来源', response.text)
                await self.pool.execute(f'UPDATE {table} SET enabled=TRUE WHERE {column}=$1', value)
        await self.history(expert, home=0)
        self.assertFalse(profitable(await self.row(expert)))

    async def test_loss_after_claim_denies_send_without_releasing_unknown_exposure(self):
        await self.healthy()
        signal, expert, _, _ = await self.signal()
        await self.enable([expert])
        await self.mode('profitable')
        await self.history(expert)
        order = await self.reserve(signal)
        work = (await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        self.assertEqual('order', work['kind'])
        await self.history(expert, home=0)
        response = await self.worker_api.post(f"/internal/executor/orders/{order['id']}/authorize", json={
            'attempt_id': work['attempt_id'], 'prepared_digest':'0'*64, 'payload_json':'{}',
            'quote_checked_at':datetime.now(timezone.utc).isoformat()})
        self.assertEqual(409, response.status_code, response.text)
        self.assertIn('近30日', response.text)
        await self.pool.execute("UPDATE v2_bet_orders SET state='unknown' WHERE id=$1", order['id'])
        result = await self.worker_api.post('/internal/executor/dispatch')
        self.assertEqual(0, result.json()['reserved'])
        self.assertEqual(Decimal('2'), await self.pool.fetchval('SELECT reserved_stake FROM v2_bet_orders WHERE id=$1', order['id']))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders WHERE signal_id=$1', signal))

    async def test_reset_all_old_effective_samples_preserves_ledger_pending_and_reverses(self):
        await self.healthy()
        pending, expert, _, _ = await self.signal()
        await self.enable([expert])
        await self.reserve(pending)
        orders_before = await self.pool.fetch('SELECT * FROM v2_bet_orders ORDER BY id')
        await self.history(expert, home=0)
        old = await self.history(expert, age_days=60)
        await self.history(expert, home=2, line='2')
        _, winner, _, _ = await self.signal()
        await self.history(winner)
        ledger_before = await self.pool.fetch('SELECT * FROM v2_signal_settlements ORDER BY signal_id,result_version')
        signals_before = await self.pool.fetch('SELECT * FROM v2_expert_signals ORDER BY id')
        plan = await preview_reset(self.pool)
        self.assertEqual([str(expert)], [e['expert_id'] for e in plan['experts']])
        self.assertEqual(2, len(plan['signals']))
        self.assertIn(str(old), [s['signal_id'] for s in plan['signals']])
        args = dict(idempotency_key='test-reset', expected_fingerprint=plan['fingerprint'],
                    as_of=datetime.fromisoformat(plan['as_of']), reason='isolated owner-requested reset')
        reset = await apply_reset(self.pool, **args)
        self.assertTrue((await apply_reset(self.pool, **args))['idempotent'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_statistics_resets'))
        self.assertEqual(ledger_before, await self.pool.fetch('SELECT * FROM v2_signal_settlements ORDER BY signal_id,result_version'))
        self.assertEqual(signals_before, await self.pool.fetch('SELECT * FROM v2_expert_signals ORDER BY id'))
        self.assertEqual(orders_before, await self.pool.fetch('SELECT * FROM v2_bet_orders ORDER BY id'))
        current = await self.row(expert)
        self.assertEqual(0, current['effective_settled_signals'])
        scope = StatisticsScope(datetime.now(timezone.utc)-timedelta(days=90), datetime.now(timezone.utc))
        summary = (await summaries(self.pool, scope.start, scope.end, scope=scope, expert_id=expert))[0]
        self.assertIsNotNone(summary['statistics_reset_at'])
        self.assertIsNone(summary['accuracy'])
        self.assertEqual(0, summary['unit_net'])
        # A corrected historical settlement remains excluded by signal identity.
        fixture = await self.pool.fetchval('SELECT fixture_id FROM v2_expert_signals WHERE id=$1', old)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await result_ledger.ingest_result(conn, ResultInput(fixture_id=fixture, provider_reference='corrected',
                    status='confirmed', home=4, away=0, source_updated_at=datetime.now(timezone.utc), evidence={'source':'test-only'}))
        self.assertEqual(0, (await self.row(expert))['effective_settled_signals'])
        await self.history(expert)
        self.assertEqual(1, (await self.row(expert))['effective_settled_signals'])
        self.assertTrue(profitable(await self.row(expert)))
        scope = StatisticsScope(scope.start, datetime.now(timezone.utc))
        self.assertEqual(2, (await trend(self.pool, expert, scope))[0]['cumulative_settled'])  # one push and one new win
        revoked = await revoke_reset(self.pool, reset_id=uuid.UUID(reset['reset_id']), reason='isolated restore')
        self.assertFalse(revoked['idempotent'])
        self.assertTrue((await revoke_reset(self.pool, reset_id=uuid.UUID(reset['reset_id']), reason='repeat restore'))['idempotent'])
        self.assertEqual(3, (await self.row(expert))['effective_settled_signals'])
        self.assertEqual('pending', await self.pool.fetchval('SELECT outcome FROM v2_expert_signals WHERE id=$1', pending))

    async def test_reset_rejects_changed_preview_without_partial_writes(self):
        _, expert, _, _ = await self.signal()
        history = await self.history(expert, home=0)
        plan = await preview_reset(self.pool)
        await self.pool.execute('DELETE FROM v2_quote_evidence WHERE reference_id=$1', history)
        with self.assertRaisesRegex(ValueError, 'statistics changed'):
            await apply_reset(self.pool, idempotency_key='stale-reset', expected_fingerprint=plan['fingerprint'],
                as_of=datetime.fromisoformat(plan['as_of']), reason='isolated stale snapshot')
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_statistics_resets'))

    async def test_concurrent_reset_same_key_has_one_audited_batch(self):
        _, expert, _, _ = await self.signal()
        await self.history(expert, home=0)
        plan = await preview_reset(self.pool)
        args = dict(idempotency_key='concurrent-reset', expected_fingerprint=plan['fingerprint'],
                    as_of=datetime.fromisoformat(plan['as_of']), reason='isolated concurrent replay')
        results = await asyncio.gather(apply_reset(self.pool, **args), apply_reset(self.pool, **args))
        self.assertEqual(results[0]['reset_id'], results[1]['reset_id'])
        self.assertEqual([False, True], sorted(r['idempotent'] for r in results))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_statistics_resets'))
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='expert_statistics_reset'"))

    async def test_pending_before_reset_settles_into_new_cycle_and_zero_profit_is_reset(self):
        pending, expert, _, _ = await self.signal()
        await self.history(expert, home=0)
        # Exact zero net is not profitable, even with effective samples.
        await self.history(expert, price='2')
        self.assertEqual(Decimal('0'), (await self.row(expert))['unit_net'])
        plan = await preview_reset(self.pool)
        self.assertEqual(2, len(plan['signals']))
        self.assertNotIn(str(pending), [s['signal_id'] for s in plan['signals']])
        await apply_reset(self.pool, idempotency_key='zero-net-reset', expected_fingerprint=plan['fingerprint'],
                          as_of=datetime.fromisoformat(plan['as_of']), reason='isolated zero-net reset')
        self.assertIsNone(await self.row(expert))
        await self.pool.execute("UPDATE v2_expert_signals SET score_basis='full_period' WHERE id=$1", pending)
        await self.pool.execute(
            "INSERT INTO v2_quote_evidence(id,evidence_kind,reference_id,fixture_id,market_id,market_name,period,line,selection,price,source_updated_at,evidence,score_baseline) "
            "SELECT $1,'expert_signal',id,fixture_id,market_id,market_name,period,line,selection,recommendation_price,quote_time,"
            "jsonb_build_object('score_basis',score_basis),score_baseline FROM v2_expert_signals WHERE id=$2", uuid.uuid4(), pending)
        fixture = await self.pool.fetchval('SELECT fixture_id FROM v2_expert_signals WHERE id=$1', pending)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await result_ledger.ingest_result(conn, ResultInput(fixture_id=fixture, provider_reference='after-reset',
                    status='confirmed', home=3, away=0, source_updated_at=datetime.now(timezone.utc), evidence={'source':'test-only'}))
        self.assertEqual(1, (await self.row(expert))['effective_settled_signals'])
        self.assertTrue(profitable(await self.row(expert)))
