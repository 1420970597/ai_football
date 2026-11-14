"""Current market snapshots retain eligibility and native values with fewer WAN reads."""
from datetime import datetime
from decimal import Decimal
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from fastapi import HTTPException

from app.routes import reviews
from app.security import encrypt_secret
import test_reviews as support


class CountedReads:
    def __init__(self, conn):
        self.conn, self.calls = conn, []

    async def fetch(self, query, *args):
        self.calls.append(query)
        return await self.conn.fetch(query, *args)

    def __getattr__(self, name):
        return getattr(self.conn, name)


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class MarketSnapshotRoundTripTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    fixture = support.ReviewTests.fixture
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    search_all = support.ReviewTests.search_all
    resolve = support.ReviewTests.resolve

    async def snapshot(self, fixture_id='fixture-1', *, settings=None, **filters):
        body = reviews.MarketInput(review_id=self.job, attempt_id=self.attempt,
                                   fixture_id=fixture_id, **filters)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                counted = CountedReads(conn)
                result = await reviews._market_snapshot(counted, body,
                    settings or SimpleNamespace(quote_max_age_seconds=60), reviews.market_filters(body))
                return result, counted.calls

    async def bind(self):
        key, _ = await self.search_all()
        await self.resolve(key)

    async def test_single_current_snapshot_preserves_native_quote_values_and_policy(self):
        await self.fixture()
        await self.bind()
        (fixture, total, quotes, max_age), calls = await self.snapshot(
            market_kind='total', period='full_time', selection='over', line=Decimal('2.5'))
        self.assertEqual(1, len(calls))
        self.assertEqual('fixture-1', fixture['id'])
        self.assertIsInstance(fixture['kickoff_at'], datetime)
        self.assertEqual((1, 1, 60), (total, len(quotes), max_age))
        self.assertIsInstance(quotes[0]['line'], Decimal)
        self.assertIsInstance(quotes[0]['price'], Decimal)
        self.assertIsInstance(quotes[0]['source_updated_at'], datetime)
        self.assertEqual(Decimal('2.5'), quotes[0]['line'])
        self.assertEqual(Decimal('1.95'), quotes[0]['price'])
        self.assertNotIn('fixture', quotes[0])
        self.assertNotIn('review_fixture_bound', quotes[0])
        self.assertNotIn('quote_age_policy', quotes[0])

    async def test_no_markets_and_filtered_no_match_preserve_distinct_complete_inventory(self):
        await self.fixture(markets=[])
        await self.bind()
        (fixture, total, quotes, _), calls = await self.snapshot()
        self.assertEqual('fixture-1', fixture['id'])
        self.assertEqual((0, []), (total, quotes))
        self.assertEqual(1, len(calls))
        await self.fixture()
        (_, total, quotes, _), calls = await self.snapshot(period='first_half')
        self.assertEqual((1, []), (total, quotes))
        self.assertEqual(1, len(calls))

    async def test_current_encrypted_policy_updates_on_next_read_and_ignores_other_keys(self):
        await self.fixture()
        await self.bind()
        settings = SimpleNamespace(encryption_key=b'k'*32, quote_max_age_seconds=60)
        await self.pool.execute("INSERT INTO v2_admin_settings(setting_key,encrypted_value) VALUES('other_key','invalid')")
        (_, _, _, age), calls = await self.snapshot(settings=settings)
        self.assertEqual((60, 1), (age, len(calls)))
        for value in ('90', '030', '3600'):
            await self.pool.execute("INSERT INTO v2_admin_settings(setting_key,encrypted_value) VALUES('quote_max_age_seconds',$1) ON CONFLICT(setting_key) DO UPDATE SET encrypted_value=excluded.encrypted_value",
                                    encrypt_secret(settings.encryption_key, value))
            (_, _, quotes, age), calls = await self.snapshot(settings=settings)
            self.assertEqual((int(value), 1, 1), (age, len(calls), len(quotes)))
            self.assertNotIn('quote_age_policy', quotes[0])
        await self.pool.execute("DELETE FROM v2_admin_settings WHERE setting_key='quote_max_age_seconds'")
        (_, _, _, age), calls = await self.snapshot(settings=settings)
        self.assertEqual((60, 1), (age, len(calls)))

    async def test_invalid_current_policy_fails_closed_and_keeps_original_error_priority(self):
        await self.fixture()
        await self.bind()
        settings = SimpleNamespace(encryption_key=b'k'*32, quote_max_age_seconds=60)
        for raw in ('invalid', *(encrypt_secret(settings.encryption_key, value) for value in ('0', '3601', '1.5'))):
            await self.pool.execute("INSERT INTO v2_admin_settings(setting_key,encrypted_value) VALUES('quote_max_age_seconds',$1) ON CONFLICT(setting_key) DO UPDATE SET encrypted_value=excluded.encrypted_value", raw)
            with self.assertRaises(HTTPException) as error:
                await self.snapshot(settings=settings)
            self.assertEqual(503, error.exception.status_code)
        with patch.object(reviews, 'MAX_MARKETS', 0), self.assertRaises(HTTPException) as error:
            await self.snapshot(settings=settings)
        self.assertEqual(413, error.exception.status_code)
        await self.pool.execute("UPDATE v2_fixtures SET phase='finished' WHERE id='fixture-1'")
        with self.assertRaises(HTTPException) as error:
            await self.snapshot(settings=settings)
        self.assertEqual((409, 'fixture is no longer active'), (error.exception.status_code, error.exception.detail))

    async def test_missing_or_other_attempt_binding_and_inactive_phase_still_fail_closed(self):
        await self.fixture()
        with self.assertRaises(HTTPException) as error:
            await self.snapshot()
        self.assertEqual((409, 'fixture must be uniquely resolved first'),
                         (error.exception.status_code, error.exception.detail))
        await self.bind()
        await self.pool.execute('UPDATE v2_review_fixture_bindings SET attempt_id=$1', uuid.uuid4())
        with self.assertRaises(HTTPException) as error:
            await self.snapshot()
        self.assertEqual('fixture must be uniquely resolved first', error.exception.detail)
        await self.pool.execute('UPDATE v2_review_fixture_bindings SET attempt_id=$1', self.attempt)
        await self.pool.execute("UPDATE v2_fixtures SET phase='finished' WHERE id='fixture-1'")
        with self.assertRaises(HTTPException) as error:
            await self.snapshot()
        self.assertEqual((409, 'fixture is no longer active'),
                         (error.exception.status_code, error.exception.detail))
        await self.pool.execute('DELETE FROM v2_review_fixture_bindings')
        with self.assertRaises(HTTPException) as error:
            await self.snapshot()
        self.assertEqual('fixture must be uniquely resolved first', error.exception.detail)

    async def test_evidence_budget_fails_before_partial_result(self):
        await self.fixture()
        await self.bind()
        with patch.object(reviews, 'MAX_MARKETS', 0), self.assertRaises(HTTPException) as error:
            await self.snapshot()
        self.assertEqual(413, error.exception.status_code)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))
