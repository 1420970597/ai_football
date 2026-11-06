"""Atomic signal/evidence behavior on isolated PostgreSQL."""
import asyncio
import os
import unittest
import uuid
from decimal import Decimal

import asyncpg

from app.routes.reviews import PickInput
from app.signal_persistence import persist_signal, persist_signals
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
                async def fetch(self, sql, *args):
                    calls.append(sql)
                    return await conn.fetch(sql, *args)
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

    async def batch(self, conn, prepared, entries=None):
        job, quote, pick = prepared
        return await persist_signals(conn, job=job, attempt_id=self.attempt,
                                     entries=entries if entries is not None else [(quote, pick, None)])

    async def test_batch_32_new_picks_save_with_one_command_and_exact_evidence(self):
        prepared = await self.prepared()
        entries = []
        for index in range(32):
            quote = {**prepared[1], 'market_id': 'batch-' + str(index), 'price': Decimal('2.08')}
            entries.append((quote, prepared[2], None))
        async with self.pool.acquire() as conn, conn.transaction():
            calls = []
            class Counted:
                def is_in_transaction(self): return conn.is_in_transaction()
                async def fetch(self, query, *args):
                    calls.append(query)
                    return await conn.fetch(query, *args)
            result = await self.batch(Counted(), prepared, entries)
        self.assertEqual(1, len(calls))
        self.assertEqual(32, len({row['id'] for row in result}))
        self.assertTrue(all(isinstance(row['id'], uuid.UUID) and isinstance(row['price'], Decimal) for row in result))
        self.assertTrue(all(not row['duplicate'] for row in result))
        rows = await self.pool.fetch('SELECT s.id,s.recommendation_price,e.price,e.evidence '
            'FROM v2_expert_signals s JOIN v2_quote_evidence e ON e.reference_id=s.id')
        self.assertEqual(32, len(rows))
        self.assertTrue(all(row['recommendation_price']==Decimal('2.08')==row['price'] for row in rows))
        self.assertTrue(all(Decimal(row['evidence']['reviewed_price'])==Decimal('1.95') for row in rows))

    async def test_batch_duplicates_keep_first_evidence_and_saved_price(self):
        prepared = await self.prepared()
        job, quote, pick = prepared
        second = pick.model_copy(update={'evidence_text': 'later duplicate'})
        entries = [(quote, pick, None), (quote, second, None)]
        async with self.pool.acquire() as conn, conn.transaction():
            first = await self.batch(conn, prepared, entries)
        self.assertEqual([False, True], [r['duplicate'] for r in first])
        self.assertEqual(first[0]['id'], first[1]['id'])
        self.assertEqual(pick.evidence_text, await self.pool.fetchval('SELECT evidence_text FROM v2_expert_signals'))
        before = await self.pool.fetchrow('SELECT * FROM v2_quote_evidence')
        quote['price'] = Decimal('3.11')
        async with self.pool.acquire() as conn, conn.transaction():
            again = await self.batch(conn, prepared, entries)
        self.assertTrue(all(r['duplicate'] for r in again))
        self.assertTrue(all(r['price']==Decimal('1.95') for r in again))
        self.assertEqual(before, await self.pool.fetchrow('SELECT * FROM v2_quote_evidence'))

    async def test_batch_waits_for_conflict_then_reads_committed_original_price(self):
        prepared = await self.prepared()
        async with self.pool.acquire() as owner, self.pool.acquire() as writer:
            transaction = owner.transaction()
            await transaction.start()
            task = None
            try:
                await self.save(owner, prepared)
                prepared[1]['price'] = Decimal('3.11')
                async def write():
                    async with writer.transaction():
                        return await self.batch(writer, prepared)
                task = asyncio.create_task(write())
                await asyncio.sleep(.05)
                self.assertFalse(task.done())
                await transaction.commit()
                result = await asyncio.wait_for(task, 2)
            finally:
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if owner.is_in_transaction(): await transaction.rollback()
        self.assertTrue(result[0]['duplicate'])
        self.assertEqual(Decimal('1.95'), result[0]['price'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))

    async def test_batch_conflict_uses_exact_unique_identity_without_mutating_metadata(self):
        prepared = await self.prepared()
        async with self.pool.acquire() as conn, conn.transaction():
            first = await self.batch(conn, prepared)
        original = await self.pool.fetchrow('SELECT * FROM v2_expert_signals')
        evidence = await self.pool.fetchrow('SELECT * FROM v2_quote_evidence')
        prepared[1].update(market_kind='spread', price=Decimal('3.11'))
        async with self.pool.acquire() as conn, conn.transaction():
            repeated = await self.batch(conn, prepared)
        self.assertEqual(first[0]['id'], repeated[0]['id'])
        self.assertTrue(repeated[0]['duplicate'])
        self.assertEqual(Decimal('1.95'), repeated[0]['price'])
        self.assertEqual(original, await self.pool.fetchrow('SELECT * FROM v2_expert_signals'))
        self.assertEqual(evidence, await self.pool.fetchrow('SELECT * FROM v2_quote_evidence'))

    async def test_batch_evidence_failure_rolls_back_every_signal(self):
        prepared = await self.prepared()
        await self.pool.execute("""CREATE FUNCTION reject_batch_evidence() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'batch evidence failure'; END $$;
            CREATE TRIGGER reject_batch_evidence BEFORE INSERT ON v2_quote_evidence
            FOR EACH ROW EXECUTE FUNCTION reject_batch_evidence()""")
        entries = [(prepared[1], prepared[2], None),
                   ({**prepared[1], 'market_id': 'second'}, prepared[2], None)]
        with self.assertRaisesRegex(asyncpg.RaiseError, 'batch evidence failure'):
            async with self.pool.acquire() as conn, conn.transaction():
                await self.batch(conn, prepared, entries)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))

    async def test_empty_batch_does_not_query_and_outer_abort_remains_atomic(self):
        prepared = await self.prepared()
        async with self.pool.acquire() as conn:
            with self.assertRaisesRegex(RuntimeError, 'requires a transaction'):
                await self.batch(conn, prepared)
            with self.assertRaisesRegex(RuntimeError, 'outer batch abort'):
                async with conn.transaction():
                    self.assertEqual([], await self.batch(conn, prepared, []))
                    await self.batch(conn, prepared)
                    raise RuntimeError('outer batch abort')
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_quote_evidence'))
