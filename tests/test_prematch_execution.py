"""Prematch requests and permits use isolated accounts and mock provider HTTP."""
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import unittest

import httpx

import test_account_protocol as protocol_fixture
import test_account_state as account_fixture
from services.collector.leyu_protocol import LeyuError
from services.executor.account_protocol import (
    AccountProtocolClient, NativeSelection, LATEST_MARKET_PATH, LIMITS_PATH, BET_PATH,
)
from services.executor.account_session import AccountScope, AccountSessionManager, RenewalHooks


class PrematchProtocolTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = protocol_fixture.AccountHTTPTests.asyncSetUp
    asyncTearDown = protocol_fixture.AccountHTTPTests.asyncTearDown
    handler = protocol_fixture.AccountHTTPTests.handler
    permit = protocol_fixture.AccountHTTPTests.permit

    async def prepare_prematch(self):
        self.responses[LATEST_MARKET_PATH] = protocol_fixture.latest()
        self.responses[LATEST_MARKET_PATH][0]['matchStatus'] = 0
        return await self.client.preflight(protocol_fixture.selection(phase='scheduled', score_benchmark=''), Decimal('2'))

    async def test_prematch_uses_type_one_for_quote_limits_and_order(self):
        prepared = await self.prepare_prematch()
        detail = prepared.payload['seriesOrders'][0]['orderDetailList'][0]
        latest = json.loads(next(r.content for r in self.requests if r.url.path == LATEST_MARKET_PATH))['idList'][0]
        limits = json.loads(next(r.content for r in self.requests if r.url.path == LIMITS_PATH))['orderMaxBetMoney'][0]
        self.assertEqual((1, 1, 1), (latest['matchType'], limits['matchType'], detail['matchType']))
        self.assertEqual('', detail['scoreBenchmark'])
        self.assertNotIn(BET_PATH, [r.url.path for r in self.requests])

    async def test_prematch_rejects_started_finished_or_unknown_market_before_limits(self):
        for status in (1, 2, 3, 110, None, 'unknown'):
            self.responses[LATEST_MARKET_PATH] = protocol_fixture.latest()
            self.responses[LATEST_MARKET_PATH][0]['matchStatus'] = status
            self.requests.clear()
            with self.subTest(status=status), self.assertRaisesRegex(LeyuError, 'selected_native_market_not_open'):
                await self.client.preflight(protocol_fixture.selection(phase='scheduled', score_benchmark=''), Decimal('2'))
            self.assertEqual([LATEST_MARKET_PATH], [r.url.path for r in self.requests])

    async def test_unknown_prematch_submission_is_never_repeated(self):
        prepared = await self.prepare_prematch()
        self.client.live_orders_enabled = True
        self.responses[BET_PATH] = httpx.ReadTimeout('isolated unknown response')
        permit = self.permit(prepared)
        from services.executor.account_protocol import SubmissionUnknown
        with self.assertRaises(SubmissionUnknown):
            await self.client.submit_once(prepared, permit)
        with self.assertRaises(LeyuError):
            await self.client.submit_once(prepared, permit)
        self.assertEqual(1, sum(r.url.path == BET_PATH for r in self.requests))

    async def test_prematch_permit_cannot_be_transferred_to_another_account(self):
        prepared = await self.prepare_prematch()
        self.client.live_orders_enabled = True
        with self.assertRaises(LeyuError):
            await self.client.submit_once(prepared, replace(self.permit(prepared), scope=protocol_fixture.scope()))
        self.assertNotIn(BET_PATH, [r.url.path for r in self.requests])


@unittest.skipUnless(account_fixture.DSN, 'requires task-owned temporary Docker database')
class PrematchOrderTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = account_fixture.AccountStateTests.asyncSetUp
    asyncTearDown = account_fixture.AccountStateTests.asyncTearDown
    recent_session = account_fixture.AccountStateTests.recent_session
    bind = account_fixture.AccountStateTests.bind
    healthy = account_fixture.AccountStateTests.healthy
    signal = account_fixture.AccountStateTests.signal
    enable = account_fixture.AccountStateTests.enable
    reserve = account_fixture.AccountStateTests.reserve
    executor = account_fixture.AccountStateTests.executor

    def native(self, request):
        response = account_fixture.AccountStateTests.native(self, request)
        if request.url.path == LATEST_MARKET_PATH:
            body = response.json()
            body['data'][0]['matchStatus'] = 0
            body['data'][0]['playId'] = getattr(self, 'play_id', '2')
            return httpx.Response(200, json=body)
        return response

    async def scheduled_signal(self, *, sport='football', market='2'):
        signal, expert, message, quote = await self.signal()
        fixture = await self.pool.fetchval('SELECT fixture_id FROM v2_expert_signals WHERE id=$1', signal)
        await self.pool.execute("UPDATE v2_fixtures SET phase='scheduled',sport=$2,kickoff_at=now()+interval '1 hour' WHERE id=$1", fixture, sport)
        await self.pool.execute("UPDATE v2_expert_signals SET phase='scheduled',market_id=$2 WHERE id=$1", signal, market)
        await self.pool.execute('UPDATE v2_current_quotes SET market_id=$2,play_id=$2 WHERE id=$1', quote, market)
        self.play_id = market
        return signal, expert, message, quote, fixture

    async def ready_order(self, **scope):
        await self.healthy()
        signal, expert, _, _, fixture = await self.scheduled_signal(**scope)
        await self.enable([expert])
        response = await self.worker_api.post('/internal/executor/dispatch')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, response.json()['reserved'])
        order = await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE signal_id=$1', signal)
        work = (await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        self.assertEqual('order', work['kind'])
        manager = AccountSessionManager(AccountScope(**work['scope']), work['config'], RenewalHooks(None, None, None))
        async with AccountProtocolClient(manager.scope, manager.venue_session(), transport=httpx.MockTransport(self.native)) as client:
            prepared = await client.preflight(NativeSelection.from_snapshot(work['selection']), order['stake'], device_imei=work['config']['leyu_app_uuid'])
        return order, work, prepared, fixture

    async def authorize(self, order, work, prepared, mutate=None):
        payload = prepared.payload
        if mutate:
            mutate(payload['seriesOrders'][0]['orderDetailList'][0])
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        prefix = f"{work['scope']['user_id']}:{work['scope']['account_id']}:{work['scope']['revision']}:"
        digest = hashlib.sha256((prefix + prepared.checked_at + ':' + encoded).encode()).hexdigest()
        return await self.worker_api.post(f"/internal/executor/orders/{order['id']}/authorize", json={
            'attempt_id': work['attempt_id'], 'prepared_digest': digest,
            'payload_json': encoded, 'quote_checked_at': prepared.checked_at})

    async def test_prematch_dispatch_snapshot_and_authorization_preserve_user_permission(self):
        order, work, prepared, _ = await self.ready_order()
        capability = (await self.api.get('/api/v1/accounts/leyu')).json()['execution_capability']
        self.assertEqual(['football', 'basketball'], capability['supported_sports'])
        self.assertEqual(['scheduled', 'live'], capability['supported_phases'])
        self.assertEqual(('supported', 'full_time_winner_total_handicap'), (capability['prematch'], capability['basketball']))
        policy = await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1', self.user['id'])
        self.assertEqual(('scheduled', '1', ''), tuple(work['selection'][k] for k in ('phase', 'sport_id', 'score_benchmark')))
        response = await self.authorize(order, work, prepared)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(policy, await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1', self.user['id']))
        self.assertEqual('submitting', await self.pool.fetchval('SELECT state FROM v2_bet_orders WHERE id=$1', order['id']))
        self.assertEqual(409, (await self.authorize(order, work, prepared)).status_code)
        evidence = await self.pool.fetch("SELECT evidence FROM v2_quote_evidence WHERE reference_id=$1 AND evidence_kind='order_attempt'", order['id'])
        self.assertEqual(['scheduled', 'scheduled'], [r['evidence']['phase'] for r in evidence])
        self.assertNotIn(BET_PATH, [r.url.path for r in self.native_requests])

    async def test_prematch_basketball_snapshot_and_authorization_use_sport_two(self):
        order, work, prepared, _ = await self.ready_order(sport='basketball', market='38')
        self.assertEqual(('scheduled', '2', '38'), tuple(work['selection'][k] for k in ('phase', 'sport_id', 'chpid')))
        response = await self.authorize(order, work, prepared)
        self.assertEqual(200, response.status_code, response.text)
        self.assertNotIn(BET_PATH, [r.url.path for r in self.native_requests])

    async def test_payload_cannot_change_match_type_or_sport(self):
        order, work, prepared, _ = await self.ready_order()
        for field, value in (('matchType', 2), ('matchType', True), ('sportId', '2')):
            with self.subTest(field=field, value=value):
                response = await self.authorize(order, work, prepared, lambda d: d.update({field: value}))
                self.assertEqual(422, response.status_code, response.text)
        self.assertEqual('pending', await self.pool.fetchval('SELECT state FROM v2_bet_orders WHERE id=$1', order['id']))
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_quote_evidence WHERE reference_id=$1 AND evidence_kind='order_attempt'", order['id']))

    async def test_prematch_to_live_transition_cannot_receive_send_permit(self):
        order, work, prepared, fixture = await self.ready_order()
        await self.pool.execute("UPDATE v2_fixtures SET phase='live' WHERE id=$1", fixture)
        response = await self.authorize(order, work, prepared)
        self.assertEqual(409, response.status_code, response.text)
        self.assertNotIn(BET_PATH, [r.url.path for r in self.native_requests])

    async def test_stale_prematch_signal_does_not_turn_into_a_live_order(self):
        await self.healthy()
        signal, expert, _, _, fixture = await self.scheduled_signal()
        await self.enable([expert])
        await self.pool.execute("UPDATE v2_fixtures SET phase='live' WHERE id=$1", fixture)
        response = await self.worker_api.post('/internal/executor/dispatch')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(0, response.json()['reserved'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_finished_cancelled_and_old_posts_never_reserve(self):
        await self.healthy()
        ids = [await self.scheduled_signal() for _ in range(3)]
        await self.enable([item[1] for item in ids])
        for (signal, _, message, _, fixture), phase in zip(ids, ('finished', 'cancelled', 'scheduled')):
            await self.pool.execute('UPDATE v2_fixtures SET phase=$2 WHERE id=$1', fixture, phase)
            if phase == 'scheduled':
                await self.pool.execute("UPDATE v2_expert_messages SET posted_at=now()-interval '6 minutes' WHERE id=$1", message)
        self.assertEqual(0, (await self.worker_api.post('/internal/executor/dispatch')).json()['reserved'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_user_disable_cancels_unsent_prematch_order(self):
        order, work, prepared, _ = await self.ready_order()
        response = await self.api.put('/api/v1/accounts/leyu/auto-betting', json={'enabled': False})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(409, (await self.authorize(order, work, prepared)).status_code)
        self.assertEqual(('cancelled', 0), tuple(await self.pool.fetchrow('SELECT state,reserved_stake FROM v2_bet_orders WHERE id=$1', order['id'])))

    async def test_unsupported_basketball_markets_cannot_fill_dispatch_limit(self):
        await self.healthy()
        rejected = [await self.scheduled_signal(sport='basketball', market='2') for _ in range(31)]
        for signal, _, _, _, _ in rejected:
            await self.pool.execute("UPDATE v2_expert_signals SET created_at=now()-interval '1 minute' WHERE id=$1", signal)
        accepted = await self.scheduled_signal()
        await self.enable([item[1] for item in rejected] + [accepted[1]])
        result = await self.worker_api.post('/internal/executor/dispatch')
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual(1, result.json()['reserved'])
        self.assertEqual([accepted[0]], [r['signal_id'] for r in await self.pool.fetch('SELECT signal_id FROM v2_bet_orders')])
        self.assertEqual([], self.native_requests)
