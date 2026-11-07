"""Final record validation reads the live quote policy with fixture locks."""
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import os
import unittest
from unittest.mock import patch
import uuid

import httpx

from app.routes import reviews
from app.security import encrypt_secret
from test_review_market_roundtrips import CountedReads
import test_reviews as support


class CountedPool:
    def __init__(self, pool):
        self.pool, self.reads = pool, []

    @asynccontextmanager
    async def acquire(self):
        async with self.pool.acquire() as conn:
            counted = CountedReads(conn)
            try:
                yield counted
            finally:
                self.reads.extend(counted.calls)


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class RecordPolicyRoundTripTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    fixture = support.ReviewTests.fixture
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    search_all = support.ReviewTests.search_all
    resolve = support.ReviewTests.resolve
    read_market = support.ReviewTests.read_market

    async def prepare_pick(self):
        fixture_id = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        quote = await self.read_market(fixture_id)
        return {'fixture_id': fixture_id, 'quote_id': quote['quote_id'], 'evidence_text': 'Alpha1 全场大2.5'}

    async def set_policy(self, value, *, encrypted=True):
        settings = self.client._transport.app.state.settings
        settings.encryption_key = b'k' * 32
        raw = encrypt_secret(settings.encryption_key, value) if encrypted else value
        await self.pool.execute(
            "INSERT INTO v2_admin_settings(setting_key,encrypted_value) VALUES('quote_max_age_seconds',$1) "
            "ON CONFLICT(setting_key) DO UPDATE SET encrypted_value=EXCLUDED.encrypted_value", raw)
        return raw

    async def test_valid_record_saves_native_price_with_one_policy_only_read(self):
        pick = await self.prepare_pick()
        counted = CountedPool(self.pool)
        with patch.object(reviews.database, 'current_pool', return_value=counted):
            response = await self.post('record_expert_signals', signals=[pick])
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([], response.json()['rejected'])
        accepted, = response.json()['accepted']
        self.assertEqual(('fixture-1', 'full_time', 'over', '2.500', '1.95000'),
                         tuple(accepted[k] for k in ('fixture_id', 'period', 'selection', 'line', 'price')))
        separate_policy_reads = [sql for sql in counted.reads if 'WHERE setting_key=ANY' in sql]
        self.assertEqual(1, len(separate_policy_reads),
                         'preflight may read the policy; final validation must share its fixture-lock read')
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertNotIn('quote_age_policy', response.text)

    async def test_live_policy_updates_and_removal_change_next_operation_without_caching(self):
        pick = await self.prepare_pick()
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=$1", self.now-timedelta(seconds=40))
        for value, accepted_count in (('20', 0), ('60', 1), ('10', 0), (None, 1)):
            with self.subTest(policy=value):
                if value is None:
                    await self.pool.execute("DELETE FROM v2_admin_settings WHERE setting_key='quote_max_age_seconds'")
                    raw = None
                else:
                    raw = await self.set_policy(value)
                response = await self.post('record_expert_signals', signals=[pick])
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(accepted_count, len(response.json()['accepted']), response.text)
                if not accepted_count:
                    self.assertEqual('quote_inactive_unknown_or_stale', response.json()['rejected'][0]['reason'])
                if raw:
                    self.assertNotIn(raw, response.text)
        self.assertTrue(response.json()['accepted'][0]['duplicate'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        traces = await self.pool.fetch('SELECT payload FROM v2_analysis_traces')
        self.assertNotIn('quote_age_policy', json.dumps([dict(row) for row in traces], default=str))

    async def test_invalid_policy_fails_closed_for_bound_and_missing_fixture(self):
        pick = await self.prepare_pick()
        for raw, encrypted in (('invalid', False), ('0', True), ('3601', True), ('1.5', True)):
            await self.set_policy(raw, encrypted=encrypted)
            for candidate in (pick, dict(pick, fixture_id='not-bound', quote_id=str(uuid.uuid4()))):
                with self.subTest(policy=raw, fixture=candidate['fixture_id']):
                    response = await self.post('record_expert_signals', signals=[candidate])
                    self.assertEqual(503, response.status_code, response.text)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))

    async def test_policy_update_during_external_refresh_is_rechecked_before_write(self):
        pick = await self.prepare_pick()
        await self.set_policy('60')
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '2 minutes'")
        self.client._transport.app.state.settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        calls = []
        factory = httpx.AsyncClient

        async def handler(request):
            self.assertEqual(self.pool.get_size(), self.pool.get_idle_size())
            calls.append(request.url.path)
            # Independent ingestion and an administrator update happen while
            # the validation transaction is released for the provider call.
            await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '30 seconds'")
            await self.set_policy('10')
            return httpx.Response(200, json={'accepted': True, 'written': True, 'written_fixtures': 1})

        with patch.object(reviews.httpx, 'AsyncClient',
                          side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            response = await self.post('record_expert_signals', signals=[pick])
        self.assertEqual(['/leyu/refresh'], calls)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([], response.json()['accepted'])
        self.assertEqual('quote_inactive_unknown_or_stale', response.json()['rejected'][0]['reason'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        await self.set_policy('60')
        response = await self.post('record_expert_signals', signals=[pick])
        self.assertEqual(1, len(response.json()['accepted']), response.text)

    async def test_missing_binding_and_unread_quote_remain_rejected_with_valid_policy(self):
        pick = await self.prepare_pick()
        await self.set_policy('60')
        for candidate in (dict(pick, quote_id=str(uuid.uuid4())), dict(pick, fixture_id='not-bound')):
            with self.subTest(candidate=candidate):
                response = await self.post('record_expert_signals', signals=[candidate])
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual([], response.json()['accepted'])
                self.assertEqual('quote_not_read_or_changed', response.json()['rejected'][0]['reason'])
        await self.pool.execute('DELETE FROM v2_review_fixture_bindings')
        response = await self.post('record_expert_signals', signals=[pick])
        self.assertEqual('quote_not_read_or_changed', response.json()['rejected'][0]['reason'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
