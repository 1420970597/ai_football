"""Dense snapshots reduce repeated lookups without changing quote contracts."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from app import ingestion
from app.quote_ingestion import upsert_quotes
import test_fixture_snapshot_atomic as fixtures
import test_quote_update_existing as existing


class DenseQuotePlanTests(unittest.IsolatedAsyncioTestCase):
    async def capture(self, counts):
        class Capture:
            async def execute(self, sql, payload):
                self.sql, self.payload = sql, payload
        conn = Capture()
        payload = [{'fixture_id': f'f-{index}'}
                   for index, count in enumerate(counts) for _ in range(count)]
        await upsert_quotes(conn, payload)
        self.assertIs(payload, conn.payload)
        return conn.sql

    async def test_prefetch_boundary_requires_density_in_every_fixture(self):
        for counts, expected in (([], False), ([1], False), ([255], False),
                                 ([256], True), ([256, 256], True),
                                 ([1000, 1], False), ([1] * 300, False)):
            with self.subTest(counts=counts):
                sql = await self.capture(counts)
                self.assertEqual(expected, 'current_rows AS MATERIALIZED' in sql)
                self.assertIn('stored.id=q.existing_id', sql)
                self.assertIn('ON CONFLICT(fixture_id,market_id,period,(COALESCE(line,0)),selection)', sql)
                self.assertIn('jsonb_to_recordset($1::jsonb)', sql)


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated task database')
class DenseQuotePostgresTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.SnapshotAtomicTests.asyncSetUp
    asyncTearDown = fixtures.SnapshotAtomicTests.asyncTearDown
    post = fixtures.SnapshotAtomicTests.post
    capture = existing.QuoteUpdateExistingTests.capture

    async def payload(self):
        stamp = (datetime.now(timezone.utc)-timedelta(seconds=10)).isoformat()
        value = fixtures.fixture(markets=[fixtures.quote(index, source_updated_at=stamp) for index in range(256)])
        self.assertEqual(200, (await self.post(value)).status_code)
        return value

    async def update_after_wait(self, offset, expected):
        value = await self.payload()
        before = await self.pool.fetch('SELECT id,market_id FROM v2_current_quotes ORDER BY market_id')
        stamp = (datetime.now(timezone.utc)+timedelta(seconds=offset)).isoformat()
        for quote in value['markets']:
            quote.update(price='2.05', source_updated_at=stamp)
        payload = await self.capture(value)
        async with self.pool.acquire() as owner, self.pool.acquire() as writer:
            transaction = owner.transaction()
            await transaction.start()
            task = None
            try:
                await owner.execute("UPDATE v2_current_quotes SET price=2.55,source_updated_at=clock_timestamp() WHERE market_id='m0'")
                task = asyncio.create_task(upsert_quotes(writer, payload))
                await asyncio.sleep(.05)
                self.assertFalse(task.done())
                await transaction.commit()
                await asyncio.wait_for(task, 2)
            finally:
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if owner.is_in_transaction():
                    await transaction.rollback()
        self.assertEqual(Decimal(expected), await self.pool.fetchval("SELECT price FROM v2_current_quotes WHERE market_id='m0'"))
        self.assertEqual(before, await self.pool.fetch('SELECT id,market_id FROM v2_current_quotes ORDER BY market_id'))
        self.assertEqual(255, await self.pool.fetchval("SELECT count(*) FROM v2_current_quotes WHERE market_id<>'m0' AND price=2.05"))

    async def test_dense_newer_concurrent_row_wins_after_update_wait(self):
        await self.update_after_wait(-2, '2.55')

    async def test_dense_newer_input_survives_concurrent_tuple_move(self):
        await self.update_after_wait(1, '2.05')

    async def test_dense_missing_reference_conflict_preserves_newer_insert(self):
        value = await self.payload()
        value['markets'].append(fixtures.quote(256, source_updated_at=(datetime.now(timezone.utc)-timedelta(seconds=2)).isoformat()))
        payload = await self.capture(value)
        async with self.pool.acquire() as owner, self.pool.acquire() as writer:
            transaction = owner.transaction()
            await transaction.start()
            task = None
            try:
                await owner.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,period,market_kind,line,selection,price,source_updated_at) VALUES(gen_random_uuid(),'f','m256','newer','full_time','handicap',0,'home',2.77,clock_timestamp())")
                expected_id = await owner.fetchval("SELECT id FROM v2_current_quotes WHERE market_id='m256'")
                task = asyncio.create_task(upsert_quotes(writer, payload))
                await asyncio.sleep(.05)
                self.assertFalse(task.done())
                await transaction.commit()
                await asyncio.wait_for(task, 2)
            finally:
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if owner.is_in_transaction():
                    await transaction.rollback()
        row = await self.pool.fetchrow("SELECT id,price FROM v2_current_quotes WHERE market_id='m256'")
        self.assertEqual((expected_id, Decimal('2.77')), tuple(row.values()))
        self.assertEqual(257, await self.pool.fetchval('SELECT count(*) FROM v2_current_quotes'))

    async def test_dense_failure_rolls_back_quote_and_parent_changes(self):
        value = await self.payload()
        before = await self.pool.fetch('SELECT * FROM v2_current_quotes ORDER BY id')
        parent = await self.pool.fetchrow('SELECT * FROM v2_fixtures')
        value['home_team'] = 'changed'
        value['markets'][0]['price'] = '2.05'
        value['markets'].append(fixtures.quote(256))
        actual = ingestion.upsert_quotes
        async def fail(conn, payload):
            await actual(conn, payload)
            raise RuntimeError('isolated dense rollback')
        with patch.object(ingestion, 'upsert_quotes', fail):
            self.assertEqual(500, (await self.post(value)).status_code)
        self.assertEqual(before, await self.pool.fetch('SELECT * FROM v2_current_quotes ORDER BY id'))
        self.assertEqual(parent, await self.pool.fetchrow('SELECT * FROM v2_fixtures'))

    async def test_dense_reference_mapping_separates_fixture_period_line_and_side(self):
        markets = [fixtures.quote(index, period=period, line=line, selection=side)
                   for index in range(16) for period in ('full_time', 'first_half')
                   for line in ('0', '0.5', '1', '1.5') for side in ('home', 'away')]
        for identity in ('f', 'g'):
            self.assertEqual(200, (await self.post(fixtures.fixture(id=identity, source_id=identity, markets=markets))).status_code)
        before = await self.pool.fetch('SELECT id,fixture_id,market_id,period,line,selection,price FROM v2_current_quotes ORDER BY id')
        changed = copy.deepcopy(markets)
        changed[-1].update(price='2.47', source_updated_at=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat())
        self.assertEqual(200, (await self.post(fixtures.fixture(id='g', source_id='g', markets=changed))).status_code)
        after = await self.pool.fetch('SELECT id,fixture_id,market_id,period,line,selection,price FROM v2_current_quotes ORDER BY id')
        self.assertEqual([row['id'] for row in before], [row['id'] for row in after])
        for old, new in zip(before, after):
            target = (old['fixture_id'], old['market_id'], old['period'], old['line'], old['selection']) == ('g', 'm15', 'first_half', Decimal('1.5'), 'away')
            self.assertEqual(Decimal('2.47') if target else old['price'], new['price'])
