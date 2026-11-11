import asyncio
from datetime import timedelta
import os
import unittest

from app.result_check_queue import claim_due_results
import test_result_ledger as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class ResultCheckPriorityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ResultLedgerTests.asyncSetUp
    asyncTearDown = support.ResultLedgerTests.asyncTearDown

    async def add(self, name, *, final=None, due_minutes=1, kickoff_hours=3,
                  phase='finished', received_days=0):
        await self.pool.execute(
            "INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) "
            "VALUES($1,$2,'football','League','Alpha','Beta',$3,$4)",
            name, 'leyu:'+name, self.now-timedelta(hours=kickoff_hours), phase)
        await self.pool.execute(
            "INSERT INTO v2_result_checks(fixture_id,next_check_at) VALUES($1,$2)",
            name, self.now-timedelta(minutes=due_minutes))
        if final:
            await self.fact(name, final, received_days=received_days)

    async def fact(self, name, status, *, received_days=0):
        await self.pool.execute(
            "INSERT INTO v2_fixture_results(fixture_id,version,provider_reference,content_hash,status,"
            "source_updated_at,evidence,received_at) SELECT $1,COALESCE(max(version),0)+1,"
            "'isolated',gen_random_uuid()::text,$2,$3,'{}',$4 FROM v2_fixture_results WHERE fixture_id=$1",
            name, status, self.now, self.now-timedelta(days=received_days))

    async def claim(self, limit=4):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                return await claim_due_results(conn, limit)

    async def test_first_facts_take_three_slots_and_corrections_keep_one(self):
        for i in range(5):
            await self.add('final'+str(i), final='confirmed', due_minutes=60-i)
            await self.add('first'+str(i), due_minutes=10-i)
        rows = await self.claim()
        self.assertEqual(['first0','first1','first2','final0'], [r['fixture_id'] for r in rows])
        self.assertEqual(4, len({r['lease_id'] for r in rows}))
        for row in rows:
            lease = await self.pool.fetchrow('SELECT lease_id,lease_until FROM v2_result_checks WHERE fixture_id=$1', row['fixture_id'])
            self.assertEqual(row['lease_id'], lease['lease_id'])
            self.assertTrue(89 <= (lease['lease_until']-self.now).total_seconds() <= 100)

    async def test_either_lane_fills_all_spare_capacity(self):
        for i in range(6):
            await self.add('first'+str(i))
        self.assertEqual(4, len(await self.claim()))
        self.assertEqual(2, len(await self.claim()))
        for i in range(6):
            await self.add('final'+str(i), final='confirmed')
        self.assertEqual(4, len(await self.claim()))
        self.assertEqual(2, len(await self.claim()))
        self.assertEqual([], await self.claim())

    async def test_small_first_lane_fills_with_corrections_and_limit_one_prioritizes_first(self):
        for i in range(4):
            await self.add('final'+str(i), final='confirmed', due_minutes=60)
        await self.add('new')
        rows = await self.claim()
        self.assertEqual(['new','final0','final1','final2'], [r['fixture_id'] for r in rows])
        await self.add('another')
        self.assertEqual(['another'], [r['fixture_id'] for r in await self.claim(1)])
        self.assertEqual(['final3'], [r['fixture_id'] for r in await self.claim(1)])

    async def test_latest_provisional_is_first_lane_and_void_is_correction_lane(self):
        await self.add('void', final='void', due_minutes=60)
        await self.add('provisional', final='confirmed', due_minutes=10)
        await self.fact('provisional', 'provisional')
        rows = await self.claim(2)
        self.assertEqual(['provisional','void'], [r['fixture_id'] for r in rows])
        self.assertEqual(3, await self.pool.fetchval('SELECT count(*) FROM v2_fixture_results'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_signal_settlements'))

    async def test_scope_backoff_and_live_leases_remain_excluded(self):
        await self.add('old', kickoff_hours=31*24)
        await self.add('recent-live', phase='live', kickoff_hours=1)
        await self.add('future', kickoff_hours=-1, phase='scheduled')
        await self.add('deferred', due_minutes=-10)
        await self.add('leased')
        await self.pool.execute("UPDATE v2_result_checks SET lease_until=now()+interval '1 minute' WHERE fixture_id='leased'")
        await self.add('confirmed-old', final='confirmed', received_days=8)
        await self.add('expired')
        await self.pool.execute("UPDATE v2_result_checks SET lease_until=now()-interval '1 second' WHERE fixture_id='expired'")
        self.assertEqual(['expired'], [r['fixture_id'] for r in await self.claim()])
        await self.add('old-final-new-provisional', final='confirmed', received_days=8)
        await self.fact('old-final-new-provisional', 'provisional')
        self.assertEqual(['old-final-new-provisional'], [r['fixture_id'] for r in await self.claim()])

    async def test_locked_rows_are_skipped_and_concurrent_claims_do_not_overlap(self):
        for i in range(12):
            await self.add('first'+str(i).zfill(2))
        async with self.pool.acquire() as locked:
            async with locked.transaction():
                await locked.fetchrow("SELECT fixture_id FROM v2_result_checks WHERE fixture_id='first00' FOR UPDATE")
                rows = await asyncio.wait_for(self.claim(), 3)
                self.assertEqual(4, len(rows))
                self.assertNotIn('first00', [r['fixture_id'] for r in rows])
        batches = await asyncio.gather(self.claim(), self.claim(), self.claim())
        ids = [r['fixture_id'] for batch in batches for r in batch]
        self.assertEqual(8, len(ids))
        self.assertEqual(8, len(set(ids)))
        self.assertIn('first00', ids)

    async def test_rolled_back_claim_does_not_consume_or_reuse_lease(self):
        await self.add('new')
        async with self.pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            first, = await claim_due_results(conn, 4)
            await tx.rollback()
        next_row, = await self.claim()
        self.assertNotEqual(first['lease_id'], next_row['lease_id'])
        self.assertEqual('new', next_row['fixture_id'])

    async def test_missing_detail_backlog_cannot_delay_fresh_finals_or_corrections(self):
        for i in range(8):
            name = 'missing' + str(i)
            await self.add(name, due_minutes=100-i)
            await self.pool.execute(
                "UPDATE v2_result_checks SET failures=50,last_reason=$2 WHERE fixture_id=$1",
                name, 'result_fixture_mismatch' if i % 2 else 'result_detail_unavailable')
        for i in range(3):
            await self.add('fresh' + str(i), due_minutes=3-i)
        await self.add('correction', final='confirmed', due_minutes=60)
        self.assertEqual(['fresh0', 'fresh1', 'missing0', 'correction'],
                         [r['fixture_id'] for r in await self.claim()])
        self.assertEqual(['fresh2', 'missing1', 'missing2', 'missing3'],
                         [r['fixture_id'] for r in await self.claim()])

    async def test_recovery_uses_spare_capacity_but_never_overrides_active_leases(self):
        for i in range(6):
            await self.add('missing' + str(i))
        await self.pool.execute("UPDATE v2_result_checks SET failures=1,last_reason='result_detail_unavailable'")
        await self.pool.execute("UPDATE v2_result_checks SET lease_until=now()+interval '1 minute' WHERE fixture_id='missing0'")
        self.assertEqual(['missing1', 'missing2', 'missing3', 'missing4'],
                         [r['fixture_id'] for r in await self.claim()])
        self.assertEqual(['missing5'], [r['fixture_id'] for r in await self.claim()])
