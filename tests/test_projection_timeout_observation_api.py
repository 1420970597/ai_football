"""Unconfirmed writes never replace post-I/O native and authorization checks."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import unittest

import httpx
import test_unchanged_quote_observation_api as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class ProjectionTimeoutObservationApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.UnchangedQuoteObservationApiTests.asyncSetUp
    asyncTearDown = support.UnchangedQuoteObservationApiTests.asyncTearDown
    context = support.UnchangedQuoteObservationApiTests.context
    post = support.UnchangedQuoteObservationApiTests.post
    fixture = support.UnchangedQuoteObservationApiTests.fixture
    search_all = support.UnchangedQuoteObservationApiTests.search_all
    resolve = support.UnchangedQuoteObservationApiTests.resolve
    prepare = support.UnchangedQuoteObservationApiTests.prepare
    observation = support.UnchangedQuoteObservationApiTests.observation
    enable_refresh = support.UnchangedQuoteObservationApiTests.enable_refresh
    transport = support.UnchangedQuoteObservationApiTests.transport

    @staticmethod
    def response(observation, **changes):
        return httpx.Response(200, json={'accepted': False, 'written': False,
            'reason': 'observed_current_projection_unconfirmed', 'snapshot_committed': False,
            'current_only': True, 'current_observation': observation, **changes})

    async def test_exact_native_read_remains_current_without_claiming_projection_commit(self):
        fixture = await self.prepare()
        self.enable_refresh()
        original, _, _ = await self.observation(fixture)
        async def handler(request):
            self.assertIsNotNone(json.loads(request.content)['known_snapshot'])
            _, _, observation = await self.observation(fixture)
            return self.response(observation)
        with self.transport(handler):
            response = await self.post('list_match_markets', fixture_id=fixture)
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertFalse(result['markets'][0]['fresh'])
        self.assertTrue(result['markets'][0]['confirmed_current'])
        self.assertTrue(result['markets'][0]['can_record'])
        self.assertFalse(result['refresh']['accepted'])
        self.assertEqual(original['source_updated_at'], await self.pool.fetchval(
            'SELECT source_updated_at FROM v2_current_quotes WHERE fixture_id=$1', fixture))

    async def test_stale_record_revalidates_exact_witness_and_persists_native_evidence(self):
        fixture = await self.prepare()
        read = await self.post('list_match_markets', fixture_id=fixture)
        quote_id = read.json()['markets'][0]['quote_id']
        self.enable_refresh()
        original, _, _ = await self.observation(fixture)
        async def handler(request):
            self.assertEqual(self.pool.get_size(), self.pool.get_idle_size())
            self.assertIsNotNone(json.loads(request.content)['known_snapshot'])
            _, _, observation = await self.observation(fixture)
            return self.response(observation)
        with self.transport(handler):
            recorded = await self.post('record_expert_signals', signals=[{
                'fixture_id': fixture, 'quote_id': quote_id, 'evidence_text': '主队让0.5'}])
        self.assertEqual(200, recorded.status_code, recorded.text)
        self.assertEqual(1, len(recorded.json()['accepted']))
        self.assertEqual('1.95000', recorded.json()['accepted'][0]['price'])
        self.assertEqual(original['source_updated_at'], await self.pool.fetchval(
            'SELECT source_updated_at FROM v2_current_quotes WHERE fixture_id=$1', fixture))
        evidence = await self.pool.fetchval('SELECT evidence FROM v2_quote_evidence')
        self.assertIsNotNone(evidence['current_confirmation'])

    async def test_incomplete_or_contradictory_current_only_envelopes_do_not_confirm(self):
        fixture = await self.prepare()
        self.enable_refresh()
        for changes in ({'accepted': True}, {'written': True}, {'current_only': False},
                        {'reason': 'untrusted_projection_reason'}):
            with self.subTest(changes=changes):
                async def handler(request):
                    _, _, observation = await self.observation(fixture)
                    return self.response(observation, **changes)
                with self.transport(handler):
                    response = await self.post('list_match_markets', fixture_id=fixture)
                self.assertEqual(200, response.status_code, response.text)
                self.assertFalse(response.json()['markets'][0]['can_record'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_expired_or_changed_native_witness_cannot_confirm_old_projection(self):
        fixture = await self.prepare()
        self.enable_refresh()
        for change in ('expired', 'price', 'context', 'foreign', 'duplicate'):
            with self.subTest(change=change):
                async def handler(request):
                    _, _, observation = await self.observation(fixture)
                    observation = deepcopy(observation)
                    if change == 'expired':
                        observation['started_at'] = observation['completed_at'] = (
                            datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat()
                    elif change == 'price': observation['quotes'][0]['price'] = '2.03'
                    elif change == 'context': observation['context']['home_score'] = 1
                    elif change == 'foreign': observation['fixture_id'] = 'another-fixture'
                    else: observation['quotes'].append(deepcopy(observation['quotes'][0]))
                    return self.response(observation)
                with self.transport(handler):
                    response = await self.post('list_match_markets', fixture_id=fixture)
                self.assertEqual(200, response.status_code, response.text)
                self.assertFalse(response.json()['markets'][0]['can_record'])

    async def test_changed_database_price_during_io_cannot_use_the_earlier_witness(self):
        fixture = await self.prepare()
        read = await self.post('list_match_markets', fixture_id=fixture)
        quote_id = read.json()['markets'][0]['quote_id']
        self.enable_refresh()
        async def handler(request):
            _, _, observation = await self.observation(fixture)
            await self.pool.execute('UPDATE v2_current_quotes SET price=2.03 WHERE fixture_id=$1', fixture)
            return self.response(observation)
        with self.transport(handler):
            result = await self.post('record_expert_signals', signals=[{
                'fixture_id': fixture, 'quote_id': quote_id, 'evidence_text': '主队让0.5'}])
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual([], result.json()['accepted'])
        self.assertEqual('quote_inactive_unknown_or_stale', result.json()['rejected'][0]['reason'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_unknown_native_timestamp_has_no_known_snapshot_or_current_permission(self):
        fixture = await self.prepare()
        await self.pool.execute('UPDATE v2_current_quotes SET source_updated_at=NULL WHERE fixture_id=$1', fixture)
        self.enable_refresh()
        async def handler(request):
            self.assertNotIn('known_snapshot', json.loads(request.content))
            _, _, observation = await self.observation(fixture)
            return self.response(observation)
        with self.transport(handler):
            response = await self.post('list_match_markets', fixture_id=fixture)
        self.assertEqual(200, response.status_code, response.text)
        self.assertFalse(response.json()['markets'][0]['can_record'])

    async def test_lease_expiry_during_io_still_prevents_recording(self):
        fixture = await self.prepare()
        read = await self.post('list_match_markets', fixture_id=fixture)
        quote_id = read.json()['markets'][0]['quote_id']
        self.enable_refresh()
        async def handler(request):
            _, _, observation = await self.observation(fixture)
            await self.pool.execute("UPDATE v2_analysis_jobs SET lease_until=now()-interval '1 second' WHERE id=$1", self.job)
            return self.response(observation)
        with self.transport(handler):
            response = await self.post('record_expert_signals', signals=[{
                'fixture_id': fixture, 'quote_id': quote_id, 'evidence_text': '主队让0.5'}])
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
