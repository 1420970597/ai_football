import asyncio
import os
import unittest
from unittest.mock import patch

import httpx

from app.routes import reviews
import test_reviews as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class RecordRefreshPreflightTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture
    search_all = support.ReviewTests.search_all
    resolve = support.ReviewTests.resolve
    read_market = support.ReviewTests.read_market

    async def prepare(self):
        fixture = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        quote = await self.read_market(fixture)
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '5 minutes'")
        self.app_settings = self.client._transport.app.state.settings
        self.app_settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        return fixture, quote

    async def record(self, fixture, quote):
        return await self.post('record_expert_signals', signals=[{
            'fixture_id': fixture, 'quote_id': quote['quote_id'], 'evidence_text': '大2.5'}])

    def transport(self, handler):
        factory = httpx.AsyncClient
        return patch.object(reviews.httpx, 'AsyncClient',
                            side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw))

    async def assert_no_writes(self):
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))

    async def test_exact_bound_pair_refreshes_once_with_no_connection_held_and_keeps_old_price(self):
        fixture, quote = await self.prepare()
        calls = []

        async def handler(request):
            self.assertEqual(self.pool.get_size(), self.pool.get_idle_size())
            calls.append(request)
            await self.pool.execute('UPDATE v2_current_quotes SET price=2.03,source_updated_at=now()')
            return httpx.Response(200, json={'accepted': True, 'reason': 'written'})

        with self.transport(handler):
            response = await self.record(fixture, quote)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, len(calls))
        self.assertEqual('2.03000', response.json()['accepted'][0]['price'])
        evidence = await self.pool.fetchval('SELECT evidence FROM v2_quote_evidence')
        self.assertEqual('1.95000', evidence['reviewed_price'])

    async def test_cross_fixture_quote_does_not_refresh_either_bound_fixture(self):
        fixture, quote = await self.prepare()
        other = await self.fixture(2)
        key, _ = await self.search_all(team='Alpha2')
        await self.resolve(key, 2)
        with self.transport(lambda request: self.fail('mismatched pair must not refresh')):
            response = await self.record(other, quote)
        self.assertEqual('quote_not_read_or_changed', response.json()['rejected'][0]['reason'])
        await self.assert_no_writes()

    async def test_unbound_quote_does_not_trigger_source_io(self):
        fixture, quote = await self.prepare()
        await self.pool.execute('DELETE FROM v2_review_fixture_bindings')
        with self.transport(lambda request: self.fail('unbound pair must not refresh')):
            response = await self.record(fixture, quote)
        self.assertEqual([], response.json()['accepted'])
        await self.assert_no_writes()

    async def test_unread_quote_does_not_trigger_source_io(self):
        fixture, quote = await self.prepare()
        await self.pool.execute('DELETE FROM v2_review_market_reads')
        with self.transport(lambda request: self.fail('unread pair must not refresh')):
            response = await self.record(fixture, quote)
        self.assertEqual([], response.json()['accepted'])
        await self.assert_no_writes()

    async def test_permission_and_lease_changes_during_io_prevent_recording(self):
        fixture, quote = await self.prepare()
        changes = [
            ('UPDATE v2_expert_sources SET enabled=false', 'UPDATE v2_expert_sources SET enabled=true'),
            ('UPDATE v2_experts SET enabled=false', 'UPDATE v2_experts SET enabled=true'),
            ('UPDATE v2_source_experts SET enabled=false', 'UPDATE v2_source_experts SET enabled=true'),
            ("UPDATE v2_analysis_jobs SET lease_until=now()-interval '1 second'",
             "UPDATE v2_analysis_jobs SET lease_until=now()+interval '5 minutes'"),
            ('UPDATE v2_expert_messages SET deleted_at=now()', 'UPDATE v2_expert_messages SET deleted_at=NULL'),
        ]
        for change, restore in changes:
            with self.subTest(change=change):
                await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '5 minutes'")

                async def handler(request):
                    self.assertEqual(self.pool.get_size(), self.pool.get_idle_size())
                    await self.pool.execute(change)
                    await self.pool.execute('UPDATE v2_current_quotes SET source_updated_at=now()')
                    return httpx.Response(200, json={'accepted': True, 'reason': 'written'})

                with self.transport(handler):
                    response = await self.record(fixture, quote)
                self.assertEqual(409, response.status_code, response.text)
                await self.assert_no_writes()
                await self.pool.execute(restore)

    async def test_new_message_revision_during_io_prevents_recording(self):
        fixture, quote = await self.prepare()

        async def handler(request):
            await self.pool.execute(
                "INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at,revision) "
                "SELECT gen_random_uuid(),source_id,expert_id,platform_message_id,'edited',posted_at,revision+1 "
                "FROM v2_expert_messages WHERE id=$1", self.message)
            await self.pool.execute('UPDATE v2_current_quotes SET source_updated_at=now()')
            return httpx.Response(200, json={'accepted': True, 'reason': 'written'})

        with self.transport(handler):
            response = await self.record(fixture, quote)
        self.assertEqual(409, response.status_code, response.text)
        await self.assert_no_writes()

    async def test_binding_revoked_and_fixture_finished_during_io_prevent_recording(self):
        fixture, quote = await self.prepare()
        for change, restore in [
            ("UPDATE v2_fixtures SET phase='finished'", "UPDATE v2_fixtures SET phase='scheduled'"),
            ('DELETE FROM v2_review_fixture_bindings', None),
        ]:
            with self.subTest(change=change):
                await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '5 minutes'")

                async def handler(request):
                    await self.pool.execute(change)
                    await self.pool.execute('UPDATE v2_current_quotes SET source_updated_at=now()')
                    return httpx.Response(200, json={'accepted': True, 'reason': 'written'})

                with self.transport(handler):
                    response = await self.record(fixture, quote)
                self.assertEqual([], response.json()['accepted'], response.text)
                await self.assert_no_writes()
                if restore:
                    await self.pool.execute(restore)

    async def test_refresh_failure_cannot_make_stale_price_usable(self):
        fixture, quote = await self.prepare()

        def handler(request):
            raise httpx.ReadTimeout('isolated timeout', request=request)

        with self.transport(handler):
            response = await self.record(fixture, quote)
        self.assertEqual('quote_inactive_unknown_or_stale', response.json()['rejected'][0]['reason'])
        await self.assert_no_writes()

    async def test_cancellation_during_io_releases_connections_and_records_nothing(self):
        fixture, quote = await self.prepare()
        entered = asyncio.Event()

        async def handler(request):
            self.assertEqual(self.pool.get_size(), self.pool.get_idle_size())
            entered.set()
            await asyncio.Event().wait()

        with self.transport(handler):
            task = asyncio.create_task(self.record(fixture, quote))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.pool.get_size(), self.pool.get_idle_size())
        await self.assert_no_writes()
