"""Collector diagnostics use the same effective quote age as recommendation API."""

import asyncio
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from fastapi import FastAPI
import httpx

from app.routes import internal
from app.security import encrypt_secret
from services.collector.worker import CollectorService,quote_freshness
from test_collector_lifecycle import FakeTelegram,FakeRuntime,FakeRealtime


class CollectorConfigQuotePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.state.settings = SimpleNamespace(collector_token='isolated-collector')
        self.app.state.decrypt_secret = lambda value: value
        self.app.include_router(internal.router)
        self.pool = SimpleNamespace(fetch=AsyncMock(return_value=[]))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://api',
            headers={'Authorization':'Bearer isolated-collector'})
        self.patch = patch('app.database.current_pool',return_value=self.pool)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.client.aclose()

    async def test_empty_runtime_rows_keep_default_without_encryption_key(self):
        response = await self.client.get('/internal/collector/config')
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual({'quote_max_age_seconds':'15'},response.json()['items'])

    async def test_config_uses_same_encrypted_runtime_value_and_hot_changes(self):
        self.app.state.settings.encryption_key = b'k'*32
        current = '45'
        async def rows(sql,keys):
            if 'quote_max_age_seconds' in keys:
                return [{'setting_key':'quote_max_age_seconds','encrypted_value':encrypt_secret(b'k'*32,current)}]
            return []
        self.pool.fetch.side_effect = rows
        first = await self.client.get('/internal/collector/config')
        self.assertEqual('45',first.json()['items']['quote_max_age_seconds'])
        current = '7'
        changed = await self.client.get('/internal/collector/config')
        self.assertEqual('7',changed.json()['items']['quote_max_age_seconds'])
        self.assertEqual({'quote_max_age_seconds'},set(changed.json()['items']))

    async def test_runtime_decrypt_failure_returns_503_not_old_or_default_value(self):
        self.app.state.settings.encryption_key = b'k'*32
        async def rows(sql,keys):
            return [{'setting_key':'quote_max_age_seconds','encrypted_value':'invalid-private-cipher'}] if 'quote_max_age_seconds' in keys else []
        self.pool.fetch.side_effect = rows
        response = await self.client.get('/internal/collector/config')
        self.assertEqual(503,response.status_code)
        self.assertNotIn('invalid-private-cipher',response.text)
        self.assertNotIn('items',response.json())

    async def test_configuration_requires_existing_collector_authorization(self):
        response = await self.client.get('/internal/collector/config',headers={'Authorization':'Bearer wrong'})
        self.assertEqual(401,response.status_code)
        self.pool.fetch.assert_not_awaited()


class CollectorQuotePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'accepted':1})))
        self.service = CollectorService(self.api,'http://api/internal','isolated-token',interval=1,
            telegram_factory=FakeTelegram,runtime_factory=FakeRuntime,realtime_factory=FakeRealtime)

    async def asyncTearDown(self):
        await self.api.aclose()

    def fixture(self,seconds=30):
        return {'id':'leyu:test','markets':[{'status':'active','source_updated_at':(datetime.now(timezone.utc)-timedelta(seconds=seconds)).isoformat()}]}

    async def config_cycle(self,config=None,error=None,telegram_error=None):
        stop = asyncio.Event()
        async def wait(*args,**kwargs): stop.set()
        self.service.telegram.sync_once.side_effect = telegram_error
        self.service.telegram.sync_once.return_value = {'state':'disabled'}
        with patch('services.collector.worker.get_config',AsyncMock(side_effect=error,return_value=config or {})), \
                patch('services.collector.worker.wait_for_work',wait):
            await self.service.telegram_loop(stop)

    async def poll_cycle(self,fixture):
        stop = asyncio.Event()
        async def collect(**kwargs):
            stop.set()
            return [fixture]
        self.service.runtime.collect.side_effect = collect
        self.service.runtime.last_report = {'complete_market_fixtures':1,'odds_requested_fixtures':1}
        async def wait(*args,**kwargs): pass
        with patch('services.collector.worker.wait_for_work',wait):
            await self.service.leyu_loop(stop)
        return dict(self.service.status['leyu'])

    async def test_hot_configuration_changes_30_second_quote_from_stale_to_fresh(self):
        await self.config_cycle({'quote_max_age_seconds':'15'})
        stale = await self.poll_cycle(self.fixture())
        self.assertEqual(('stale',0,'partial',15),(stale['freshness'],stale['fresh_active_quotes'],stale['state'],stale['quote_max_age_seconds']))
        await self.config_cycle({'quote_max_age_seconds':'45'})
        fresh = await self.poll_cycle(self.fixture())
        self.assertEqual(('fresh',1,'healthy',45),(fresh['freshness'],fresh['fresh_active_quotes'],fresh['state'],fresh['quote_max_age_seconds']))

    async def test_config_failure_invalidates_previous_healthy_threshold(self):
        await self.config_cycle({'quote_max_age_seconds':'45'})
        self.assertEqual('healthy',(await self.poll_cycle(self.fixture()))['state'])
        await self.config_cycle(error=httpx.ReadTimeout('private-http-error'))
        health = self.service.status['leyu']
        self.assertEqual(('unknown',0,'partial',None),(health['freshness'],health['fresh_active_quotes'],health['state'],health['quote_max_age_seconds']))
        unknown = await self.poll_cycle(self.fixture())
        self.assertEqual('unknown',unknown['freshness'])
        self.assertNotEqual('healthy',unknown['state'])
        self.assertFalse(self.service.quote_policy_available)

    async def test_invalid_config_never_uses_stale_policy_but_tg_failure_preserves_loaded_policy(self):
        for value in ('0','3601','1.5','true','１２'):
            with self.subTest(value=value):
                await self.config_cycle({'quote_max_age_seconds':value})
                self.assertFalse(self.service.quote_policy_available)
        await self.config_cycle({'quote_max_age_seconds':'7'},telegram_error=RuntimeError('private-telegram-failure'))
        self.assertTrue(self.service.quote_policy_available)
        self.assertEqual(7,self.service.quote_max_age_seconds)

    async def test_startup_and_missing_source_timestamp_never_claim_fresh(self):
        initial = await self.poll_cycle(self.fixture(1))
        self.assertEqual('unknown',initial['freshness'])
        self.assertNotEqual('healthy',initial['state'])
        await self.config_cycle({'quote_max_age_seconds':'15'})
        unknown = await self.poll_cycle({'id':'leyu:test','markets':[{'status':'active','source_updated_at':None}]})
        self.assertEqual('unknown',unknown['freshness'])
        self.assertNotEqual('healthy',unknown['state'])

    def test_exact_runtime_age_and_future_time_match_api_freshness_contract(self):
        now = datetime(2026,10,27,tzinfo=timezone.utc)
        fixtures = [{'markets':[{'status':'active','source_updated_at':(now-timedelta(seconds=15)).isoformat()},
            {'status':'active','source_updated_at':(now-timedelta(seconds=15,microseconds=1)).isoformat()},
            {'status':'active','source_updated_at':(now+timedelta(microseconds=1)).isoformat()},
            {'status':'suspended','source_updated_at':now.isoformat()}]}]
        health = quote_freshness(fixtures,now=now,max_age_seconds=15)
        self.assertEqual((1,2,1),(health['fresh_active_quotes'],health['known_time_active_quotes'],health['unknown_time_active_quotes']))
        self.assertEqual('partial',health['freshness'])
        self.assertEqual(0,quote_freshness(fixtures,now=now,max_age_seconds=None)['fresh_active_quotes'])
