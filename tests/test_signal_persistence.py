"""Atomic signal/evidence behavior on isolated PostgreSQL."""
import os
import unittest
import uuid
from decimal import Decimal

import asyncpg

from app.routes.reviews import PickInput
from app.signal_persistence import persist_signal
import test_reviews as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class SignalPersistenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    fixture = support.ReviewTests.fixture
    search_all = support.ReviewTests.search_all
    resolve = support.ReviewTests.resolve
    read_market = support.ReviewTests.read_market
    context = support.ReviewTests.context
    post = support.ReviewTests.post

    async def prepared(self):
        fixture = await self.fixture()
        key, _ = await self.search_all()
        await self.resolve(key)
        read = await self.read_market(fixture)
        quote = await self.pool.fetchrow('SELECT q.*,f.phase,f.home_score,f.away_score,'
            'f.half_home_score,f.half_away_score,q.price AS read_price,'
            'q.source_updated_at AS read_source_time FROM v2_current_quotes q '
            'JOIN v2_fixtures f ON f.id=q.fixture_id WHERE q.id=$1', uuid.UUID(read['quote_id']))
        job = await self.pool.fetchrow('SELECT * FROM v2_analysis_jobs WHERE id=$1', self.job)
        pick = PickInput(fixture_id=fixture, quote_id=read['quote_id'], evidence_text='Alpha 大2.5 @9.99')
        return job, dict(quote), pick

    async def save(self, conn, prepared):
        job, quote, pick = prepared
        return await persist_signal(conn, job=job, attempt_id=self.attempt, quote=quote,
                                    pick=pick, confirmation=None)

    async def test_single_command_preserves_price_read_price_context_and_reference(self):
        prepared = await self.prepared()
        prepared[1]['price'] = Decimal('2.08')
        async with self.pool.acquire() as conn, conn.transaction():
            calls = []
            class Counted:
                def is_in_transaction(self): return conn.is_in_transaction()
                async def fetchrow(self, sql, *args):
                    calls.append(sql)
                    return await conn.fetchrow(sql, *args)
            result = await self.save(Counted(), prepared)
        self.assertEqual(1, len(calls))
        signal = await self.pool.fetchrow('SELECT * FROM v2_expert_signals')
        evidence = await self.pool.fetchrow('SELECT * FROM v2_quote_evidence')
        self.assertEqual(signal['id'], result['id'])
        self.assertEqual(signal['id'], evidence['reference_id'])
        self.assertEqual(Decimal('2.08'), signal['recommendation_price'])
        self.assertEqual(signal['recommendation_price'], evidence['price'])
        self.assertEqual(Decimal('1.95'), Decimal(evidence['evidence']['reviewed_price']))
        self.assertEqual(signal['score_baseline'], evidence['score_baseline'])
        for key in ('fixture_id', 'market_id', 'market_name', 'period', 'line', 'selection'):
            self.assertEqual(signal[key], evidence[key])
        self.assertEqual(str(prepared[2].quote_id), evidence['evidence']['quote_id'])
        self.assertEqual(str(self.attempt), evidence['evidence']['attempt_id'])

    async def test_duplicate_does_not_rewrite_original_snapshot(self):
        prepared = await self.prepared()
        async with self.pool.acquire() as conn, conn.transaction():
            await self.save(conn, prepared)
        before = await self.pool.fetchrow('SELECT * FROM v2_quote_evidence')
        prepared[1]['price'] = Decimal('3.11')
        async with self.pool.acquire() as conn, conn.transaction():
            self.assertIsNone(await self.save(conn, prepared))
        self.assertEqual(before, await self.pool.fetchrow('SELECT * FROM v2_quote_evidence'))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_evidence_failure_leaves_no_signal(self):
        prepared = await self.prepared()
        await self.pool.execute("""CREATE FUNCTION reject_test_evidence() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test evidence failure'; END $$;
            CREATE TRIGGER reject_test_evidence BEFORE INSERT ON v2_quote_evidence
            FOR EACH ROW EXECUTE FUNCTION reject_test_evidence()""")
        with self.assertRaisesRegex(asyncpg.RaiseError, 'test evidence failure'):
            async with self.pool.acquire() as conn, conn.transaction():
                await self.save(conn, prepared)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))

    async def test_outer_abort_rolls_back_signal_and_evidence(self):
        prepared = await self.prepared()
        with self.assertRaisesRegex(RuntimeError, 'abort outer'):
            async with self.pool.acquire() as conn, conn.transaction():
                await self.save(conn, prepared)
                raise RuntimeError('abort outer')
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))

    async def test_caller_transaction_required(self):
        prepared = await self.prepared()
        async with self.pool.acquire() as conn:
            with self.assertRaisesRegex(RuntimeError, 'requires a transaction'):
                await self.save(conn, prepared)
