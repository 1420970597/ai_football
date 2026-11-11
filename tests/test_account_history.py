"""Read-only history contracts; no provider access or funded actions."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import asyncio
import os
import unittest
import uuid

from services.api.app.account_history import summarize
from services.api.app.routes import account_history
from services.collector.leyu_protocol import LeyuError
from services.executor.account_history import scan
from services.executor.worker import Executor
import test_account_state as support


NOW = datetime.now(timezone.utc)


def receipt(order='one', state='settled', **values):
    return {'order_no': order, 'identifier_kind': 'order', 'state': state, 'match_id': '123',
            'stake': '100', 'payout': '120', 'price': '1.95', 'net_profit': '20',
            'pnl_basis': 'provider_reported_net', 'fees': None, 'currency': 'CNY',
            'placed_at': (NOW-timedelta(hours=1)).isoformat(), 'settled_at': None,
            'market_name': '全场大小', 'selection_name': '大', **values}


def page(items=(), more=False, **values):
    return {'items': list(items), 'has_more': more, 'malformed_records': 0,
            'conflicting_order_ids': [], **values}


class HistoryScanTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, responses, max_pages=10):
        client = type('Client', (), {'orders': AsyncMock(side_effect=responses)})()
        result = await scan(client, NOW-timedelta(days=7), NOW, max_pages=max_pages)
        return result, client

    async def test_both_states_and_all_pages_settled_supersedes_unsettled(self):
        (items, complete, error), client = await self.collect([
            page([receipt(state='unsettled')], True), page([receipt('pending', 'unsettled')]),
            page([receipt()]),
        ])
        self.assertTrue(complete)
        self.assertEqual('', error)
        self.assertEqual({'one': 'settled', 'pending': 'unsettled'}, {i['order_no']: i['state'] for i in items})
        self.assertEqual(['unsettled', 'unsettled', 'settled'], [c.args[0] for c in client.orders.call_args_list])

    async def test_repeated_pages_stop_and_cannot_prove_absence(self):
        (items, complete, error), client = await self.collect([
            page([receipt(state='unsettled')], True), page([receipt(state='unsettled')], True), page()])
        self.assertFalse(complete)
        self.assertEqual('history_page_limit', error)
        self.assertEqual(3, client.orders.call_count)
        self.assertEqual(1, len(items))

    async def test_bad_dates_missing_and_outside_are_excluded(self):
        (items, complete, _), _ = await self.collect([page(), page([
            receipt('missing', placed_at=None), receipt('old', placed_at=(NOW-timedelta(days=8)).isoformat()), receipt()])])
        self.assertFalse(complete)
        self.assertEqual(['one'], [i['order_no'] for i in items])

    async def test_conflicting_duplicate_is_not_a_financial_fact(self):
        (items, complete, _), _ = await self.collect([
            page(), page([receipt()], True), page([receipt(net_profit='99')])])
        self.assertFalse(complete)
        self.assertEqual([], items)

    async def test_parser_conflicts_and_malformed_are_propagated(self):
        (items, complete, _), _ = await self.collect([page(), page([receipt()], malformed_records=1, conflicting_order_ids=['one'])])
        self.assertFalse(complete)
        self.assertEqual([], items)

    async def test_partial_failure_and_page_budget(self):
        (items, complete, error), _ = await self.collect([
            page([receipt('pending', 'unsettled')]), LeyuError('account_transport_failure')])
        self.assertEqual(1, len(items))
        self.assertFalse(complete)
        self.assertEqual('account_transport_failure', error)
        (_, complete, error), _ = await self.collect([page([receipt()], True), page()], max_pages=1)
        self.assertFalse(complete)
        self.assertEqual('history_page_limit', error)

    def test_projection_pending_unknown_currency_and_missing_profit(self):
        result = summarize([receipt(), receipt('usd', currency='USD', net_profit='-3'),
                            receipt('pending', 'unsettled', net_profit='9000'),
                            receipt('unknown', net_profit=None), receipt('unmapped', match_id=None)], True)
        self.assertFalse(result['association_complete'])
        match = result['matches'][0]
        self.assertEqual((1, 3), (match['unsettled_count'], match['settled_count']))
        self.assertEqual({'CNY': '20', 'USD': '-3'}, {m['currency']: m['settled_net'] for m in match['currencies']})
        self.assertEqual(1, match['unknown_profit_count'])
        self.assertEqual(1, result['unmapped_order_count'])
        self.assertTrue(summarize([], True)['association_complete'])
        self.assertFalse(summarize([], False)['association_complete'])
        unknown = summarize([receipt(currency=None)], True)['matches'][0]['currencies'][0]
        self.assertIsNone(unknown['settled_net'])
        self.assertEqual(1, unknown['unknown_profit_count'])


@unittest.skipUnless(os.environ.get('ACCOUNT_TEST_DATABASE_URL'), 'requires task-owned Docker database')
class HistoryStateTests(unittest.IsolatedAsyncioTestCase):
    recent_session = support.AccountStateTests.recent_session
    bind = support.AccountStateTests.bind
    healthy = support.AccountStateTests.healthy

    async def asyncSetUp(self):
        await support.AccountStateTests.asyncSetUp(self)
        self.app.include_router(account_history.router)
        self.app.include_router(account_history.executor_router)
        self.history_auth = patch.object(account_history, 'require_user', AsyncMock(side_effect=lambda request: dict(self.user)))
        self.history_auth.start()

    async def asyncTearDown(self):
        self.history_auth.stop()
        await support.AccountStateTests.asyncTearDown(self)

    async def refresh(self):
        response = await self.api.post('/api/v1/accounts/history/refresh', json={})
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    async def claim(self):
        await self.healthy()
        await self.refresh()
        response = await self.worker_api.post('/internal/executor/claim')
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def finish_body(self, work, **values):
        return {'attempt_id': work['attempt_id'], 'complete': True, 'items': [receipt()],
                'observed_at': datetime.now(timezone.utc).isoformat(), **values}

    async def test_homepage_request_deduplicated_and_end_to_end_read_only(self):
        account = await self.healthy()
        results = await asyncio.gather(*(self.refresh() for _ in range(6)))
        self.assertTrue(all(r['status'] == 'queued' for r in results))
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_account_jobs WHERE kind='history'"))
        work = (await self.worker_api.post('/internal/executor/claim')).json()
        self.assertEqual({'leyu_api_base', 'leyu_origin', 'leyu_request_id'}, set(work['config']))
        self.assertEqual([], work['orders'])
        calls = []

        class ReadOnlyClient:
            def __init__(inner, scope, session):
                self.assertEqual(str(account['id']), scope.account_id)
                self.assertEqual('own-business-session', session.request_id)

            async def __aenter__(inner): return inner
            async def __aexit__(inner, *args): pass
            async def orders(inner, state, **query):
                calls.append((state, query))
                return page([receipt('pending', 'unsettled')] if state == 'unsettled' else [receipt()])

        await Executor(self.worker_api, 'https://platform.invalid/internal/executor', client_factory=ReadOnlyClient).account_job(work)
        data = (await self.api.get('/api/v1/accounts/history')).json()
        self.assertEqual('completed', data['status'])
        self.assertEqual(2, data['order_count'])
        self.assertEqual('20', data['matches'][0]['currencies'][0]['settled_net'])
        self.assertEqual(2, len(calls))
        self.assertEqual(timedelta(days=7), calls[0][1]['end']-calls[0][1]['begin'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))
        self.assertEqual('completed', (await self.refresh())['status'])
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_account_jobs WHERE kind='history'"))

    async def test_requires_own_verified_account_and_busy_does_not_relogin(self):
        self.assertEqual('unbound', (await self.refresh())['status'])
        account = await self.bind()
        self.assertEqual('verification_required', (await self.refresh())['status'])
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_account_jobs WHERE kind='history'"))
        await self.healthy()
        await self.pool.execute("INSERT INTO v2_account_jobs(id,user_id,account_id,binding_revision,kind) VALUES($1,$2,$3,2,'verify')",
                                uuid.uuid4(), self.user['id'], account['id'])
        self.assertEqual('account_busy', (await self.refresh())['status'])

    async def test_receipt_idempotence_and_conflict(self):
        work = await self.claim()
        url = f"/internal/executor/jobs/{work['job_id']}/history/finish"
        body = self.finish_body(work)
        for _ in range(2):
            response = await self.worker_api.post(url, json=body)
            self.assertEqual(200, response.status_code, response.text)
        body['items'] = []
        self.assertEqual(409, (await self.worker_api.post(url, json=body)).status_code)

    async def test_history_job_cannot_renew_or_complete_with_balance_only(self):
        import hashlib
        work = await self.claim()
        root = f"/internal/executor/jobs/{work['job_id']}"
        response = await self.worker_api.post(root+'/renewal/claim', json={
            'attempt_id': work['attempt_id'], 'expected_revision': work['scope']['revision'],
            'identity_hash': hashlib.sha256(b'2001:own-user').hexdigest()})
        self.assertEqual(409, response.status_code, response.text)
        response = await self.worker_api.post(root+'/finish', json={
            'attempt_id': work['attempt_id'], 'status': 'success', 'balance': '100',
            'observed_at': datetime.now(timezone.utc).isoformat()})
        self.assertEqual(409, response.status_code, response.text)
        account = await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE user_id=$1', self.user['id'])
        self.assertEqual('ready', account['renewal_state'])
        self.assertEqual('healthy', account['health_state'])
        self.assertEqual('running', await self.pool.fetchval('SELECT state FROM v2_account_jobs WHERE id=$1', uuid.UUID(work['job_id'])))

    async def test_window_duplicates_and_forged_attempt_rejected(self):
        work = await self.claim()
        url = f"/internal/executor/jobs/{work['job_id']}/history/finish"
        for changes, expected in (
            ({'attempt_id': str(uuid.uuid4())}, 409),
            ({'items': [receipt(placed_at=(NOW-timedelta(days=8)).isoformat())]}, 422),
            ({'items': [receipt(), receipt()]}, 422),
            ({'items': [receipt(net_profit='NaN')]}, 422),
            ({'complete': True, 'error_category': 'transport_failure'}, 422),
        ):
            response = await self.worker_api.post(url, json=self.finish_body(work, **changes))
            self.assertEqual(expected, response.status_code, response.text)
        self.assertIn((await self.api.post(url, json=self.finish_body(work))).status_code, (401, 403))

    async def test_rebinding_hides_old_records_and_rejects_late_worker(self):
        work = await self.claim()
        previous_user = self.user
        self.user = {**previous_user, 'id': uuid.uuid4()}
        self.assertEqual('unbound', (await self.api.get('/api/v1/accounts/history')).json()['status'])
        self.user = previous_user
        await self.pool.execute('UPDATE v2_leyu_accounts SET binding_revision=binding_revision+1 WHERE user_id=$1', self.user['id'])
        self.assertEqual('not_loaded', (await self.api.get('/api/v1/accounts/history')).json()['status'])
        response = await self.worker_api.post(f"/internal/executor/jobs/{work['job_id']}/history/finish", json=self.finish_body(work))
        self.assertEqual(409, response.status_code)

    async def test_partial_failure_is_cached_and_lease_expiry_fails_closed(self):
        work = await self.claim()
        response = await self.worker_api.post(f"/internal/executor/jobs/{work['job_id']}/history/finish", json=self.finish_body(
            work, complete=False, items=[], error_category='account_transport_failure'))
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('partial', (await self.refresh())['status'])
        self.assertFalse((await self.api.get('/api/v1/accounts/history')).json()['association_complete'])
        await self.pool.execute("UPDATE v2_account_history SET requested_at=now()-interval '61 seconds'")
        await self.refresh()
        work = (await self.worker_api.post('/internal/executor/claim')).json()
        await self.pool.execute("UPDATE v2_account_jobs SET lease_until=now()-interval '1 second' WHERE id=$1", uuid.UUID(work['job_id']))
        response = await self.worker_api.post(f"/internal/executor/jobs/{work['job_id']}/history/finish", json=self.finish_body(work))
        self.assertEqual(409, response.status_code, response.text)
