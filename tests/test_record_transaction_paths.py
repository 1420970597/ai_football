"""Count real PostgreSQL transaction boundaries and retain rejection/rollback behavior."""
from contextlib import asynccontextmanager
import os
import unittest
from unittest.mock import patch

from app import database
from app.routes import reviews
import test_reviews as support


class CountedConnection:
    def __init__(self, connection, events):
        self.connection, self.events = connection, events

    @asynccontextmanager
    async def transaction(self):
        self.events.append('begin')
        try:
            async with self.connection.transaction():
                yield
        except BaseException:
            self.events.append('rollback')
            raise
        else:
            self.events.append('commit')

    def __getattr__(self, name):
        return getattr(self.connection, name)


class CountedPool:
    def __init__(self, pool):
        self.pool, self.events = pool, []

    @asynccontextmanager
    async def acquire(self, *args, **kwargs):
        async with self.pool.acquire(*args, **kwargs) as connection:
            yield CountedConnection(connection, self.events)


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class RecordTransactionPathTests(unittest.IsolatedAsyncioTestCase):
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
        self.client._transport.app.state.settings.market_refresh_url = 'http://collector:8092/leyu/refresh'
        return fixture, quote

    async def record(self, fixture, quote):
        return await self.post('record_expert_signals', signals=[{
            'fixture_id': fixture, 'quote_id': quote['quote_id'], 'evidence_text': '大2.5'}])

    async def assert_no_signals(self):
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))

    async def test_fresh_quote_commits_once_and_duplicate_keeps_immutable_evidence(self):
        fixture, quote = await self.prepare()
        counted = CountedPool(self.pool)
        with patch.object(database, 'current_pool', return_value=counted):
            response = await self.record(fixture, quote)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(['begin', 'commit'], counted.events)
        self.assertFalse(response.json()['accepted'][0]['duplicate'])
        counted.events.clear()
        with patch.object(database, 'current_pool', return_value=counted):
            duplicate = await self.record(fixture, quote)
        self.assertEqual(['begin', 'commit'], counted.events)
        self.assertTrue(duplicate.json()['accepted'][0]['duplicate'])
        self.assertEqual(response.json()['accepted'][0]['signal_id'], duplicate.json()['accepted'][0]['signal_id'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))

    async def test_stale_quote_releases_preflight_transaction_during_io(self):
        fixture, quote = await self.prepare()
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '5 minutes'")
        counted = CountedPool(self.pool)

        async def refresh(*args):
            self.assertEqual(['begin', 'commit'], counted.events)
            self.assertEqual(self.pool.get_size(), self.pool.get_idle_size())
            await self.pool.execute('UPDATE v2_current_quotes SET source_updated_at=now(),price=2.03')
            return {'attempted': True, 'accepted': True, 'reason': 'written'}

        with patch.object(database, 'current_pool', return_value=counted), \
                patch.object(reviews, '_request_market_refresh', side_effect=refresh):
            response = await self.record(fixture, quote)
        self.assertEqual(['begin', 'commit', 'begin', 'commit'], counted.events)
        self.assertEqual('2.03000', response.json()['accepted'][0]['price'])
        self.assertEqual('1.95000', (await self.pool.fetchval('SELECT evidence FROM v2_quote_evidence'))['reviewed_price'])

    async def test_finalizer_rechecks_permission_and_current_quote_after_preflight(self):
        fixture, quote = await self.prepare()
        original = reviews._record_current_signals
        cases = [
            ('UPDATE v2_expert_sources SET enabled=false', 'UPDATE v2_expert_sources SET enabled=true', 409, None),
            ("UPDATE v2_fixtures SET phase='finished'", "UPDATE v2_fixtures SET phase='scheduled'", 200, 'quote_or_score_context_changed'),
            ("UPDATE v2_current_quotes SET status='suspended'", "UPDATE v2_current_quotes SET status='active'", 200, 'quote_inactive_unknown_or_stale'),
            ("UPDATE v2_current_quotes SET source_updated_at=now()-interval '5 minutes'", 'UPDATE v2_current_quotes SET source_updated_at=now()', 200, 'quote_inactive_unknown_or_stale'),
        ]
        for change, restore, status, reason in cases:
            with self.subTest(change=change):
                async def changed(conn, body, request, observations):
                    self.assertTrue(conn.is_in_transaction())
                    await self.pool.execute(change)
                    return await original(conn, body, request, observations)

                with patch.object(reviews, '_record_current_signals', side_effect=changed):
                    response = await self.record(fixture, quote)
                self.assertEqual(status, response.status_code, response.text)
                if reason:
                    self.assertEqual(reason, response.json()['rejected'][0]['reason'])
                await self.assert_no_signals()
                await self.pool.execute(restore)

    async def test_finalizer_snapshots_current_price_without_matching_expert_price(self):
        fixture, quote = await self.prepare()
        original = reviews._record_current_signals

        async def changed(conn, body, request, observations):
            await self.pool.execute('UPDATE v2_current_quotes SET price=2.15')
            return await original(conn, body, request, observations)

        with patch.object(reviews, '_record_current_signals', side_effect=changed):
            response = await self.record(fixture, quote)
        self.assertEqual('2.15000', response.json()['accepted'][0]['price'])
        self.assertEqual('1.95000', (await self.pool.fetchval('SELECT evidence FROM v2_quote_evidence'))['reviewed_price'])

    async def test_receipt_failure_rolls_back_signal_quote_and_releases_transaction(self):
        fixture, quote = await self.prepare()
        await self.pool.execute("CREATE FUNCTION reject_record_receipt() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.trace_kind='mcp_record_expert_signals' THEN RAISE EXCEPTION 'isolated receipt failure'; END IF; RETURN NEW; END $$")
        await self.pool.execute('CREATE TRIGGER reject_record_receipt BEFORE INSERT ON v2_analysis_traces FOR EACH ROW EXECUTE FUNCTION reject_record_receipt()')
        counted = CountedPool(self.pool)
        with patch.object(database, 'current_pool', return_value=counted):
            with self.assertRaisesRegex(Exception, 'isolated receipt failure'):
                await self.record(fixture, quote)
        self.assertEqual(['begin', 'rollback'], counted.events)
        await self.assert_no_signals()
        self.assertEqual(self.pool.get_size(), self.pool.get_idle_size())
        await self.pool.execute('DROP TRIGGER reject_record_receipt ON v2_analysis_traces')
        self.assertEqual(1, len((await self.record(fixture, quote)).json()['accepted']))
