"""Real PostgreSQL stable-identity and concurrent recheck behavior."""
import asyncio
import copy
import os
import unittest
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from unittest.mock import patch

from pydantic import ValidationError
from app import ingestion
from app.routes.internal import FixtureInput
from app.quote_ingestion import upsert_quotes
import test_fixture_snapshot_atomic as fixtures


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated task database')
class QuoteUpdateExistingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.SnapshotAtomicTests.asyncSetUp
    asyncTearDown=fixtures.SnapshotAtomicTests.asyncTearDown
    post=fixtures.SnapshotAtomicTests.post

    async def payload(self):
        value=fixtures.fixture(markets=[fixtures.quote(0)])
        stamp=(datetime.now(timezone.utc)-timedelta(seconds=10)).isoformat()
        value['markets'][0]['source_updated_at']=stamp
        self.assertEqual(200,(await self.post(value)).status_code)
        return value

    async def test_mixed_updates_inserts_and_replays_preserve_ids_and_xmin(self):
        value=await self.payload()
        original=await self.pool.fetchrow('SELECT id,xmin::text AS version FROM v2_current_quotes')
        value['markets'][0].update(price='2.05',source_updated_at=datetime.now(timezone.utc).isoformat())
        value['markets'].append(fixtures.quote(1))
        self.assertEqual(200,(await self.post(value)).status_code)
        rows=await self.pool.fetch('SELECT id,market_id,price,xmin::text AS version FROM v2_current_quotes ORDER BY market_id')
        self.assertEqual(original['id'],rows[0]['id'])
        self.assertEqual(Decimal('2.05'),rows[0]['price'])
        self.assertNotEqual(original['version'],rows[0]['version'])
        self.assertEqual(200,(await self.post(value)).status_code)
        self.assertEqual(rows,await self.pool.fetch('SELECT id,market_id,price,xmin::text AS version FROM v2_current_quotes ORDER BY market_id'))

    def test_null_zero_reference_collision_rejected_before_any_write(self):
        value=fixtures.fixture(markets=[fixtures.quote(0,kind='winner',line=None),fixtures.quote(0,kind='total',line='0')])
        with self.assertRaisesRegex(ValidationError,'duplicate quote reference'):
            FixtureInput.model_validate(value)

    async def test_failure_after_upsert_rolls_back_updates_inserts_and_parent(self):
        value=await self.payload()
        before=await self.pool.fetch('SELECT * FROM v2_current_quotes')
        parent=await self.pool.fetchrow('SELECT * FROM v2_fixtures')
        value['markets'][0]['price']='2.05';value['markets'].append(fixtures.quote(1));value['home_team']='changed'
        actual=ingestion.upsert_quotes
        async def fail(conn,payload):
            await actual(conn,payload)
            raise RuntimeError('isolated rollback after SQL')
        with patch.object(ingestion,'upsert_quotes',fail):
            self.assertEqual(500,(await self.post(value)).status_code)
        self.assertEqual(before,await self.pool.fetch('SELECT * FROM v2_current_quotes'))
        self.assertEqual(parent,await self.pool.fetchrow('SELECT * FROM v2_fixtures'))

    async def capture(self,value):
        class Capture:
            async def execute(self,sql,payload):self.payload=payload
        captured=Capture()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                with patch.object(ingestion,'upsert_quotes',lambda c,p:upsert_quotes(captured,p)):
                    await ingestion.ingest_fixture_batch(conn,[FixtureInput.model_validate(value)])
        return captured.payload

    async def test_concurrent_newer_existing_row_wins_after_update_wait(self):
        value=await self.payload();value['markets'][0].update(price='2.05',source_updated_at=(datetime.now(timezone.utc)-timedelta(seconds=2)).isoformat())
        payload=await self.capture(value)
        async with self.pool.acquire() as owner,self.pool.acquire() as writer:
            transaction=owner.transaction();await transaction.start();task=None
            try:
                await owner.execute("UPDATE v2_current_quotes SET price=2.55,source_updated_at=clock_timestamp()")
                task=asyncio.create_task(upsert_quotes(writer,payload))
                await asyncio.sleep(.05)
                self.assertFalse(task.done())
                await transaction.commit()
                await asyncio.wait_for(task,2)
            finally:
                if task and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
                if owner.is_in_transaction():await transaction.rollback()
        self.assertEqual(Decimal('2.55'),await self.pool.fetchval('SELECT price FROM v2_current_quotes'))

    async def test_waited_update_still_applies_newer_input_after_tuple_moves(self):
        value=await self.payload()
        value['markets'][0].update(price='2.05',source_updated_at=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat())
        payload=await self.capture(value)
        async with self.pool.acquire() as owner,self.pool.acquire() as writer:
            transaction=owner.transaction();await transaction.start();task=None
            try:
                await owner.execute("UPDATE v2_current_quotes SET price=2.55,source_updated_at=clock_timestamp()")
                task=asyncio.create_task(upsert_quotes(writer,payload))
                await asyncio.sleep(.05)
                self.assertFalse(task.done())
                await transaction.commit()
                await asyncio.wait_for(task,2)
            finally:
                if task and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
                if owner.is_in_transaction():await transaction.rollback()
        self.assertEqual(Decimal('2.05'),await self.pool.fetchval('SELECT price FROM v2_current_quotes'))

    async def test_concurrent_insert_conflict_rechecks_newer_row_and_identity(self):
        value=await self.payload();value['markets']=[fixtures.quote(1,source_updated_at=(datetime.now(timezone.utc)-timedelta(seconds=2)).isoformat())]
        payload=await self.capture(value)
        async with self.pool.acquire() as owner,self.pool.acquire() as writer:
            transaction=owner.transaction();await transaction.start();task=None
            try:
                await owner.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,period,market_kind,line,selection,price,source_updated_at) VALUES(gen_random_uuid(),'f','m1','newer','full_time','handicap',0,'home',2.77,clock_timestamp())")
                expected=await owner.fetchval("SELECT id FROM v2_current_quotes WHERE market_id='m1'")
                task=asyncio.create_task(upsert_quotes(writer,payload));await asyncio.sleep(.05)
                self.assertFalse(task.done());await transaction.commit();await asyncio.wait_for(task,2)
            finally:
                if task and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
                if owner.is_in_transaction():await transaction.rollback()
        row=await self.pool.fetchrow("SELECT id,price FROM v2_current_quotes WHERE market_id='m1'")
        self.assertEqual((expected,Decimal('2.77')),tuple(row.values()))

    async def test_reference_mapping_keeps_fixture_period_line_and_selection_separate(self):
        markets=[fixtures.quote(0,period=period,line=line,selection=side)
                 for period in ('full_time','first_half')for line in ('0','0.5')for side in ('home','away')]
        for identity in ('f','g'):
            self.assertEqual(200,(await self.post(fixtures.fixture(id=identity,source_id=identity,markets=markets))).status_code)
        before=await self.pool.fetch('SELECT id,fixture_id,period,line,selection,price FROM v2_current_quotes ORDER BY id')
        stamp=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
        changed=copy.deepcopy(markets)
        changed[-1].update(price='2.47',source_updated_at=stamp)
        self.assertEqual(200,(await self.post(fixtures.fixture(id='g',source_id='g',markets=changed))).status_code)
        after=await self.pool.fetch('SELECT id,fixture_id,period,line,selection,price FROM v2_current_quotes ORDER BY id')
        self.assertEqual([r['id']for r in before],[r['id']for r in after])
        for old,new in zip(before,after):
            target=(old['fixture_id'],old['period'],old['line'],old['selection'])==('g','first_half',Decimal('.5'),'away')
            self.assertEqual(Decimal('2.47')if target else old['price'],new['price'])

    async def test_full_mixed_batch_preserves_unchanged_and_old_versions_and_existing_ids(self):
        value=fixtures.chunked(2000)
        stamp=(datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat()
        for chunk in value['market_chunks']:
            for quote in chunk:quote['source_updated_at']=stamp
        self.assertEqual(200,(await self.post(value)).status_code)
        before={r['market_id']:r for r in await self.pool.fetch('SELECT id,market_id,price,xmin::text AS version FROM v2_current_quotes')}
        changed=copy.deepcopy(value)
        for index,quote in enumerate(q for chunk in changed['market_chunks']for q in chunk):
            if 500<=index<1000:
                quote.update(price='2.11',source_updated_at=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat())
            elif 1000<=index<1500:
                quote.update(price='2.37',source_updated_at=datetime.now(timezone.utc).isoformat())
            elif index>=1500:quote['market_id']='new-'+quote['market_id']
        changed['markets_complete']=False
        self.assertEqual(200,(await self.post(changed)).status_code)
        after={r['market_id']:r for r in await self.pool.fetch('SELECT id,market_id,price,xmin::text AS version FROM v2_current_quotes')}
        self.assertEqual(2500,len(after))
        for index in range(2000):
            key='m'+str(index)
            self.assertEqual(before[key]['id'],after[key]['id'])
            if 1000<=index<1500:
                self.assertEqual(Decimal('2.37'),after[key]['price'])
                self.assertNotEqual(before[key]['version'],after[key]['version'])
            else:self.assertEqual(before[key],after[key])
