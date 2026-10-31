"""Independent quote timestamps cannot be swallowed by stale fixture metadata."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import unittest

import test_fixture_snapshot_atomic as snapshot_fixture
from app.ingestion import ingest_fixture_batch
from app.routes.internal import FixtureInput


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated task database')
class IndependentQuoteIngestionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = snapshot_fixture.SnapshotAtomicTests.asyncSetUp
    asyncTearDown = snapshot_fixture.SnapshotAtomicTests.asyncTearDown
    post = snapshot_fixture.SnapshotAtomicTests.post

    def snapshot(self,fixture_seconds,quote_seconds,**updates):
        now=datetime.now(timezone.utc)
        fixture_time=(now+timedelta(seconds=fixture_seconds)).isoformat()
        quote_time=(now+timedelta(seconds=quote_seconds)).isoformat()
        quote=snapshot_fixture.quote(0,source_updated_at=quote_time,received_at=now.isoformat())
        return snapshot_fixture.fixture(phase='live',home_score=1,away_score=0,
            source_updated_at=fixture_time,received_at=now.isoformat(),markets=[quote],**updates)

    async def test_old_parent_accepts_new_quote_using_stored_identity_without_metadata_regression(self):
        first=self.snapshot(-10,-30)
        self.assertEqual(200,(await self.post(first)).status_code)
        original=await self.pool.fetchrow('SELECT id,source_updated_at,home_score FROM v2_fixtures')
        quote_id=await self.pool.fetchval('SELECT id FROM v2_current_quotes')
        stale=self.snapshot(-20,-1)
        stale.update(id='untrusted-new-identity',home_team='Old team',home_score=0,phase='scheduled')
        stale['markets'][0]['price']='2.05'
        response=await self.post(stale)
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual({'accepted':0,'ignored_stale':1},response.json())
        fixture=await self.pool.fetchrow('SELECT id,source_updated_at,home_score FROM v2_fixtures')
        self.assertEqual(original,fixture)
        quote=await self.pool.fetchrow('SELECT id,fixture_id,price,source_updated_at FROM v2_current_quotes')
        self.assertEqual((quote_id,'f',Decimal('2.05')),(quote['id'],quote['fixture_id'],quote['price']))
        self.assertEqual(FixtureInput.model_validate(stale).markets[0].source_updated_at,quote['source_updated_at'])

    async def test_new_parent_does_not_regress_old_quote(self):
        first=self.snapshot(-20,-2)
        self.assertEqual(200,(await self.post(first)).status_code)
        old=await self.pool.fetchrow('SELECT id,price,source_updated_at FROM v2_current_quotes')
        fresh=self.snapshot(-1,-30)
        fresh['home_score']=2
        fresh['markets'][0]['price']='1.50'
        response=await self.post(fresh)
        self.assertEqual(1,response.json()['accepted'])
        self.assertEqual(2,await self.pool.fetchval('SELECT home_score FROM v2_fixtures'))
        self.assertEqual(old,await self.pool.fetchrow('SELECT id,price,source_updated_at FROM v2_current_quotes'))

    async def test_stale_complete_snapshot_updates_present_quote_but_never_closes_absent(self):
        first=self.snapshot(-10,-30)
        first['markets'].append(snapshot_fixture.quote(1))
        self.assertEqual(200,(await self.post(first)).status_code)
        stale=self.snapshot(-20,-1)
        stale['markets'][0]['price']='2.05'
        response=await self.post(stale)
        self.assertEqual(0,response.json()['accepted'])
        rows=await self.pool.fetch('SELECT market_id,price,status FROM v2_current_quotes ORDER BY market_id')
        self.assertEqual([('m0',Decimal('2.05'),'active'),('m1',Decimal('1.95'),'active')],[tuple(row.values()) for row in rows])

    async def test_finished_fixture_never_reopens_even_with_newer_live_parent_and_new_option(self):
        first=self.snapshot(-20,-30)
        first['phase']='finished'
        self.assertEqual(200,(await self.post(first)).status_code)
        terminal=await self.pool.fetchrow('SELECT phase,source_updated_at,home_score FROM v2_fixtures')
        newer=self.snapshot(-1,0)
        newer['markets'].append(snapshot_fixture.quote(1))
        response=await self.post(newer)
        self.assertEqual(0,response.json()['accepted'])
        self.assertEqual(terminal,await self.pool.fetchrow('SELECT phase,source_updated_at,home_score FROM v2_fixtures'))
        self.assertEqual(2,await self.pool.fetchval('SELECT count(*) FROM v2_current_quotes'))
        self.assertTrue(all(row['status']=='closed' for row in await self.pool.fetch('SELECT status FROM v2_current_quotes')))

    async def test_newer_scheduled_parent_cannot_regress_live_status_but_quote_remains_independent(self):
        self.assertEqual(200,(await self.post(self.snapshot(-20,-30))).status_code)
        newer=self.snapshot(-1,0)
        newer['phase']='scheduled'
        newer['markets'][0]['price']='2.10'
        response=await self.post(newer)
        self.assertEqual(0,response.json()['accepted'])
        self.assertEqual('live',await self.pool.fetchval('SELECT phase FROM v2_fixtures'))
        self.assertEqual(Decimal('2.10'),await self.pool.fetchval('SELECT price FROM v2_current_quotes'))

    async def test_repeated_terminal_snapshot_does_not_rewrite_already_closed_quote(self):
        initial = self.snapshot(-20, -30)
        self.assertEqual(200, (await self.post(initial)).status_code)
        terminal = self.snapshot(-10, -30)
        terminal.update(phase='finished', markets=[], markets_complete=False)
        self.assertEqual(200, (await self.post(terminal)).status_code)
        before = await self.pool.fetchrow('SELECT id,status,xmin::text version,ctid::text location,received_at,source_updated_at FROM v2_current_quotes')
        self.assertEqual('closed', before['status'])
        self.assertEqual(200, (await self.post(terminal)).status_code)
        after = await self.pool.fetchrow('SELECT id,status,xmin::text version,ctid::text location,received_at,source_updated_at FROM v2_current_quotes')
        self.assertEqual(before, after)

    async def test_concurrent_confirmed_result_lock_prevents_inflight_quote_reopening(self):
        self.assertEqual(200,(await self.post(self.snapshot(-20,-30))).status_code)
        blocker=await self.pool.acquire()
        transaction=blocker.transaction()
        await transaction.start()
        task=None
        try:
            await blocker.execute("UPDATE v2_fixtures SET phase='finished',match_state='finished' WHERE id='f'")
            await blocker.execute("UPDATE v2_current_quotes SET status='closed' WHERE fixture_id='f'")
            incoming=self.snapshot(-1,0)
            incoming['markets'].append(snapshot_fixture.quote(1))
            task=asyncio.create_task(self.post(incoming))
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task),.1)
            await transaction.commit()
            response=await asyncio.wait_for(task,5)
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual(0,response.json()['accepted'])
            self.assertEqual('finished',await self.pool.fetchval('SELECT phase FROM v2_fixtures'))
            self.assertTrue(all(row['status']=='closed' for row in await self.pool.fetch('SELECT status FROM v2_current_quotes')))
        finally:
            if blocker.is_in_transaction():
                await transaction.rollback()
            await self.pool.release(blocker)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)


if __name__=='__main__':
    unittest.main()
