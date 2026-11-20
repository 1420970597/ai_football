"""Post-I/O authorization and native quote checks still govern skipped uploads."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import patch
import httpx

from app.routes import reviews
from app.quote_observation import CONTEXT_FIELDS, DETAIL_PATH
from quote_witness import known_quote_snapshot
import test_reviews as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class UnchangedQuoteObservationApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture
    search_all = support.ReviewTests.search_all
    resolve = support.ReviewTests.resolve

    async def prepare(self):
        fixture_id = await self.fixture(markets=[{'market_id': '4', 'market_name': '全场让球',
            'kind': 'handicap', 'period': 'full_time', 'line': '-0.5', 'selection': 'home',
            'price': '1.95', 'status': 'active', 'source_updated_at': (self.now-timedelta(hours=1)).isoformat(),
            'received_at': self.now.isoformat(), 'play_id': '4',
            'market_instance_id': 'native-market', 'selection_id': 'native-option'}])
        key, _ = await self.search_all()
        await self.resolve(key)
        self.client._transport.app.state.settings.quote_max_age_seconds = 15
        return fixture_id

    def transport(self, handler):
        factory = httpx.AsyncClient
        return patch.object(reviews.httpx, 'AsyncClient',
            side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw))

    async def observation(self, fixture_id):
        quote = dict(await self.pool.fetchrow('SELECT * FROM v2_current_quotes WHERE fixture_id=$1', fixture_id))
        fixture = dict(await self.pool.fetchrow('SELECT * FROM v2_fixtures WHERE id=$1', fixture_id))
        stamp = datetime.now(timezone.utc).isoformat()
        observation = {'path': DETAIL_PATH, 'started_at': stamp, 'completed_at': stamp,
            'fixture_id': fixture_id, 'source_match_id': fixture_id,
            'context': {key: fixture[key] for key in CONTEXT_FIELDS},
            'quotes': [json.loads(json.dumps(quote, default=str))]}
        return quote, fixture, observation

    def enable_refresh(self):
        self.client._transport.app.state.settings.market_refresh_url = 'http://collector:8092/leyu/refresh'

    @staticmethod
    def response(observation):
        return httpx.Response(200, json={'accepted': False, 'written': False,
            'reason': 'observed_unchanged', 'snapshot_committed': False,
            'current_only': True, 'current_observation': observation})

    async def test_same_observation_reads_and_records_without_rewriting_native_quote_time(self):
        fixture_id = await self.prepare()
        self.enable_refresh()
        calls = []
        original, _, _ = await self.observation(fixture_id)
        async def handler(request):
            self.assertEqual(self.pool.get_size(), self.pool.get_idle_size())
            quote, fixture, observation = await self.observation(fixture_id)
            self.assertEqual(known_quote_snapshot(fixture, [quote]), json.loads(request.content)['known_snapshot'])
            calls.append(request)
            return self.response(observation)
        with self.transport(handler):
            read = await self.post('list_match_markets', fixture_id=fixture_id)
            self.assertEqual(200, read.status_code, read.text)
            quote = read.json()['markets'][0]
            self.assertTrue(quote['confirmed_current'])
            self.assertFalse(quote['fresh'])
            self.assertFalse(read.json()['refresh']['accepted'])
            recorded = await self.post('record_expert_signals', signals=[{
                'fixture_id': fixture_id, 'quote_id': quote['quote_id'], 'evidence_text': '主队让0.5'}])
        self.assertEqual(200, recorded.status_code, recorded.text)
        self.assertEqual('1.95000', recorded.json()['accepted'][0]['price'])
        self.assertEqual(1, len(calls))
        current = await self.pool.fetchval('SELECT source_updated_at FROM v2_current_quotes WHERE fixture_id=$1', fixture_id)
        self.assertEqual(original['source_updated_at'], current)
        evidence = await self.pool.fetchval('SELECT evidence FROM v2_quote_evidence')
        self.assertIsNotNone(evidence['current_confirmation'])

    async def test_stale_record_preflight_supplies_only_the_selected_native_quotes(self):
        fixture_id = await self.prepare()
        read = await self.post('list_match_markets', fixture_id=fixture_id)
        quote = read.json()['markets'][0]
        self.enable_refresh()
        calls = []
        async def handler(request):
            self.assertEqual(self.pool.get_size(), self.pool.get_idle_size())
            native, fixture, observation = await self.observation(fixture_id)
            self.assertEqual(known_quote_snapshot(fixture, [native]), json.loads(request.content)['known_snapshot'])
            calls.append(request)
            return self.response(observation)
        with self.transport(handler):
            recorded = await self.post('record_expert_signals', signals=[{
                'fixture_id': fixture_id, 'quote_id': quote['quote_id'], 'evidence_text': '主队让0.5'}])
        self.assertEqual(200, recorded.status_code, recorded.text)
        self.assertEqual(1, len(recorded.json()['accepted']))
        self.assertEqual(1, len(calls))

    async def test_same_marker_without_valid_native_observation_is_not_current(self):
        fixture_id = await self.prepare()
        self.enable_refresh()
        _, _, original = await self.observation(fixture_id)
        bad = [None, original | {'context': {}}, original | {'path': '/not-detail'},
               original | {'started_at': (datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat()}]
        changed = deepcopy(original)
        changed['quotes'][0]['price'] = '2.03'
        bad.append(changed)
        for observation in bad:
            with self.subTest(missing=observation is None):
                with self.transport(lambda request: self.response(observation)):
                    read = await self.post('list_match_markets', fixture_id=fixture_id)
                self.assertEqual(200, read.status_code, read.text)
                self.assertFalse(read.json()['markets'][0]['can_record'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_attempt_or_source_disabled_during_observation_still_rejected(self):
        fixture_id = await self.prepare()
        self.enable_refresh()
        for sql in ("UPDATE v2_analysis_jobs SET lease_until=now()-interval '1 second'",
                    'UPDATE v2_expert_sources SET enabled=false'):
            with self.subTest(change=sql):
                await self.pool.execute("UPDATE v2_analysis_jobs SET lease_until=now()+interval '5 minutes'")
                await self.pool.execute('UPDATE v2_expert_sources SET enabled=true')
                async def handler(request):
                    _, _, observation = await self.observation(fixture_id)
                    await self.pool.execute(sql)
                    return self.response(observation)
                with self.transport(handler):
                    read = await self.post('list_match_markets', fixture_id=fixture_id)
                self.assertEqual(409, read.status_code, read.text)
                self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))
                self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_quote_changed_during_observation_cannot_use_the_old_receipt(self):
        fixture_id = await self.prepare()
        self.enable_refresh()
        async def handler(request):
            _, _, observation = await self.observation(fixture_id)
            await self.pool.execute('UPDATE v2_current_quotes SET price=2.03')
            return self.response(observation)
        with self.transport(handler):
            read = await self.post('list_match_markets', fixture_id=fixture_id)
        self.assertEqual(200, read.status_code, read.text)
        current_price = await self.pool.fetchval('SELECT price FROM v2_current_quotes WHERE fixture_id=$1', fixture_id)
        self.assertEqual('2.03000', str(current_price))
        self.assertFalse(read.json()['markets'][0]['can_record'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
