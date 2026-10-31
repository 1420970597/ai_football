"""闭盘保留演练仅删恢复目标的无引用投影，真实临时PG事务验证。"""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import unittest
import uuid

import asyncpg

from infra.recovery.bundle import RecoveryError, audit_inventory, restore_bundle
from infra.recovery.retention import apply_retention, plan_retention
import test_recovery as support


class RetentionConnectionGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_role_rejected_before_table_read_lock_or_delete(self):
        class ProductionConnection:
            def __init__(self):
                self.calls = []

            async def fetchrow(self, query):
                self.calls.append(query)
                return {'role': 'platform_api', 'name': 'production'}

        conn = ProductionConnection()
        for operation in (lambda: plan_retention(conn, cutoff=datetime.now(timezone.utc)),
                          lambda: apply_retention(conn, {})):
            with self.assertRaisesRegex(RecoveryError, 'only_explicit_isolated'):
                await operation()
        self.assertEqual(2, len(conn.calls))
        self.assertTrue(all('current_user' in query for query in conn.calls))


@unittest.skipUnless(os.environ.get('RECOVERY_TEST_ADMIN_URL'), 'requires explicit task-owned temporary recovery PostgreSQL')
class RetentionRehearsalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.RecoveryIntegrationTests.asyncSetUp
    asyncTearDown = support.RecoveryIntegrationTests.asyncTearDown
    populate = support.RecoveryIntegrationTests.populate
    bundle = support.RecoveryIntegrationTests.bundle

    async def quote(self, suffix, *, status='closed', quality='known', phase='finished',
                    old=True, fixture=None, identity=None):
        fixture = fixture or 'retention:' + suffix
        identity = identity or uuid.uuid4()
        stamp = datetime.now(timezone.utc) - timedelta(days=60 if old else 1)
        await self.source.execute(
            "INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase,"
            "source_updated_at,received_at,source_time_quality) VALUES($1,$1,'football','隔离联赛','甲','乙',$2,$3,$2,$2,$4) "
            "ON CONFLICT(id) DO NOTHING", fixture, stamp, phase, quality)
        await self.source.execute(
            "INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,line,selection,price,source_updated_at,"
            "received_at,source_time_quality,status,market_kind) VALUES($1,$2,$3,'独赢',NULL,'home',1.95,$4,$4,$5,$6,'winner')",
            identity, fixture, suffix, stamp, quality, status)
        return identity

    async def restore(self):
        stream, manifest = await self.bundle()
        objects = support.MemoryObjects()
        report = await restore_bundle(self.target, stream, self.backup_key, objects)
        self.assertEqual({}, report['differences'])
        self.assertEqual(manifest['inventory'], await audit_inventory(self.target))
        self.assertEqual(self.objects.items, objects.items)
        return report

    async def plan(self, **kwargs):
        return await plan_retention(self.target, cutoff=datetime.now(timezone.utc) - timedelta(days=30, seconds=5), **kwargs)

    async def all_quotes(self, conn):
        return {str(row['id']): row['value'] for row in await conn.fetch('SELECT id,to_jsonb(q)::text AS value FROM v2_current_quotes q ORDER BY id')}

    async def test_restore_rehearsal_preserves_all_evidence_balances_returns_and_referenced_closed(self):
        safe = [await self.quote('safe-a'), await self.quote('safe-b')]
        fk = await self.quote('foreign-key')
        nested = await self.quote('nested-json')
        fixture_evidence = await self.quote('fixture-result')
        for suffix, changes in [('recent', {'old': False}), ('active', {'status': 'active'}),
                                ('unknown-time', {'quality': 'unknown'}), ('live', {'phase': 'live'})]:
            await self.quote(suffix, **changes)
        # Existing FK and arbitrary JSON columns are discovered from the restored
        # schema. The JSON contains compact uppercase UUID evidence in an array.
        for conn in (self.source, self.target):
            await conn.execute('CREATE TABLE v2_retention_extra_fk(quote_id uuid REFERENCES v2_current_quotes(id))')
            await conn.execute('CREATE TABLE v2_retention_extra_json(id int PRIMARY KEY,evidence jsonb)')
        await self.source.execute('INSERT INTO v2_retention_extra_fk VALUES($1)', fk)
        await self.source.execute('INSERT INTO v2_retention_extra_json VALUES(1,$1)', {'nested': [{'raw': nested.hex.upper()}]})
        order = await self.source.fetchval("SELECT id FROM v2_bet_orders WHERE state='settled'")
        await self.source.execute("UPDATE v2_bet_orders SET quote_snapshot=$2 WHERE id=$1",
                                  order, {'quote_id': str(nested), 'price': '1.95', 'source': 'isolated-snapshot'})
        await self.source.execute("INSERT INTO v2_order_receipt_evidence(id,order_id,receipt_sha256,payload) VALUES($1,$2,$3,$4)",
            uuid.uuid4(), order, 'a'*64, {'test_only': True, 'reported_net': '1.90', 'currency': 'CNY'})
        await self.source.execute("INSERT INTO v2_fixture_results(fixture_id,version,provider_reference,content_hash,status,"
            "source_updated_at,evidence) VALUES('retention:fixture-result',1,'isolated-official','test-only','confirmed',now(),'{}')")
        await self.source.execute('UPDATE v2_leyu_accounts SET available_balance=123.45')
        report = await self.restore()
        before = await self.all_quotes(self.target)
        manifest = await self.plan()
        self.assertEqual(set(map(str, safe)), {row['id'] for row in manifest['rows']})
        self.assertEqual(3, manifest['protected'])
        self.assertGreater(len(manifest['protected_checkpoint']['table_hashes']), 40)
        self.assertEqual('123.45', manifest['protected_checkpoint']['account_balances']['known_balance_total'])
        result = await apply_retention(self.target, manifest)
        self.assertEqual('rehearsed', result['state'])
        self.assertEqual(2, result['deleted'])
        self.assertEqual(result['before'], result['after'])
        self.assertEqual(report['inventory_after'], await audit_inventory(self.target))
        after = await self.all_quotes(self.target)
        self.assertEqual({key: value for key, value in before.items() if key not in set(map(str, safe))}, after)
        self.assertIn(str(fk), after)
        self.assertIn(str(nested), after)
        self.assertIn(str(fixture_evidence), after)
        self.assertEqual(before, await self.all_quotes(self.source))
        again = await apply_retention(self.target, manifest)
        self.assertEqual(('already_absent', 0), (again['state'], again['deleted']))
        self.assertEqual(after, await self.all_quotes(self.target))

    async def test_501_candidates_use_stable_500_manifest_and_cursor_without_truncation(self):
        for index in range(501):
            await self.quote('batch-' + str(index), fixture='retention:batch', identity=uuid.UUID(int=index+1))
        await self.restore()
        first = await self.plan()
        self.assertEqual(500, len(first['rows']))
        self.assertTrue(first['has_more'])
        self.assertEqual(str(uuid.UUID(int=500)), first['next_after'])
        self.assertEqual(500, (await apply_retention(self.target, first))['deleted'])
        second = await self.plan(after=uuid.UUID(first['next_after']))
        self.assertEqual([str(uuid.UUID(int=501))], [row['id'] for row in second['rows']])
        self.assertFalse(second['has_more'])
        self.assertEqual(1, (await apply_retention(self.target, second))['deleted'])
        self.assertEqual(0, await self.target.fetchval('SELECT count(*) FROM v2_current_quotes'))
        with self.assertRaisesRegex(RecoveryError, 'candidate_budget_invalid'):
            await self.plan(limit=501)

    async def test_source_and_admin_database_are_rejected_without_mutation(self):
        await self.quote('guard')
        before = await self.all_quotes(self.source)
        with self.assertRaisesRegex(RecoveryError, 'requires_owned_recovery_target'):
            await plan_retention(self.source, cutoff=datetime.now(timezone.utc)-timedelta(days=31))
        with self.assertRaisesRegex(RecoveryError, 'only_explicit_isolated'):
            await apply_retention(self.admin, {})
        self.assertEqual(before, await self.all_quotes(self.source))

    async def test_new_nested_json_reference_after_manifest_aborts_whole_batch(self):
        ids = [await self.quote('race-json-a'), await self.quote('race-json-b')]
        await self.restore()
        manifest = await self.plan()
        before = await self.all_quotes(self.target)
        await self.target.execute("UPDATE v2_audit_log SET details=$1", {'trace': {'quote': str(ids[0])}})
        with self.assertRaisesRegex(RecoveryError, 'new_reference_conflict'):
            await apply_retention(self.target, manifest)
        self.assertEqual(before, await self.all_quotes(self.target))
        self.assertEqual(str(ids[0]), await self.target.fetchval("SELECT details->'trace'->>'quote' FROM v2_audit_log"))

    async def test_concurrent_new_fk_times_out_then_preserves_committed_reference(self):
        quote = await self.quote('race-fk')
        await self.quote('race-fk-safe')
        await self.restore()
        # Use an existing incoming FK: review evidence may be written by a task
        # between manifest production and the transaction's reference lock.
        message = await self.target.fetchval('SELECT id FROM v2_expert_messages LIMIT 1')
        job, attempt = uuid.uuid4(), uuid.uuid4()
        await self.target.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key) VALUES($1,$2,'succeeded',$3)", job, message, str(job))
        manifest = await self.plan()
        peer = await asyncpg.connect(self.source_dsn.replace(self.names[0], self.names[1]))
        try:
            transaction = peer.transaction()
            await transaction.start()
            await peer.execute('INSERT INTO v2_review_market_reads(job_id,attempt_id,quote_id) VALUES($1,$2,$3)', job, attempt, quote)
            before = await self.all_quotes(self.target)
            with self.assertRaises(asyncpg.LockNotAvailableError):
                await apply_retention(self.target, manifest)
            self.assertEqual(before, await self.all_quotes(self.target))
            await transaction.commit()
            with self.assertRaisesRegex(RecoveryError, 'new_reference_conflict'):
                await apply_retention(self.target, manifest)
            self.assertEqual(before, await self.all_quotes(self.target))
            self.assertEqual(1, await self.target.fetchval('SELECT count(*) FROM v2_review_market_reads WHERE quote_id=$1', quote))
        finally:
            await peer.close()

    async def test_status_timestamp_price_or_fixture_change_aborts_original_manifest(self):
        quote = await self.quote('changed')
        await self.quote('unchanged')
        await self.restore()
        manifest = await self.plan()
        for sql in ("UPDATE v2_current_quotes SET status='active' WHERE id=$1",
                    "UPDATE v2_current_quotes SET price=2.1 WHERE id=$1",
                    "UPDATE v2_current_quotes SET source_time_quality='unknown' WHERE id=$1",
                    "UPDATE v2_current_quotes SET received_at=now() WHERE id=$1",
                    "UPDATE v2_fixtures SET phase='live' WHERE id=(SELECT fixture_id FROM v2_current_quotes WHERE id=$1)"):
            transaction = self.target.transaction()
            await transaction.start()
            try:
                await self.target.execute(sql, quote)
                before = await self.all_quotes(self.target)
                with self.assertRaisesRegex(RecoveryError, 'original_row_changed'):
                    await apply_retention(self.target, manifest)
                self.assertEqual(before, await self.all_quotes(self.target))
            finally:
                await transaction.rollback()

    async def test_post_delete_financial_trigger_is_detected_and_entire_transaction_rolls_back(self):
        await self.quote('rollback')
        await self.restore()
        await self.target.execute('UPDATE v2_leyu_accounts SET available_balance=50')
        await self.target.execute("CREATE FUNCTION retention_test_balance_change() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "UPDATE v2_leyu_accounts SET available_balance=available_balance+1; RETURN OLD; END $$")
        await self.target.execute('CREATE TRIGGER retention_test_after_delete AFTER DELETE ON v2_current_quotes FOR EACH ROW EXECUTE FUNCTION retention_test_balance_change()')
        manifest = await self.plan()
        before = await self.all_quotes(self.target)
        with self.assertRaisesRegex(RecoveryError, 'protected_state_changed'):
            await apply_retention(self.target, manifest)
        self.assertEqual(before, await self.all_quotes(self.target))
        self.assertEqual(Decimal('50.00'), await self.target.fetchval('SELECT available_balance FROM v2_leyu_accounts'))
        self.assertEqual(manifest['protected_checkpoint']['inventory'], await audit_inventory(self.target))

    async def test_partial_missing_or_tampered_manifest_never_deletes_remaining_candidate(self):
        ids = [await self.quote('partial-a'), await self.quote('partial-b')]
        await self.restore()
        manifest = await self.plan()
        corrupt = copy.deepcopy(manifest)
        corrupt['rows'][0]['id'] = str(uuid.uuid4())
        with self.assertRaisesRegex(RecoveryError, 'manifest_invalid'):
            await apply_retention(self.target, corrupt)
        await self.target.execute('DELETE FROM v2_current_quotes WHERE id=$1', ids[0])
        before = await self.all_quotes(self.target)
        with self.assertRaisesRegex(RecoveryError, 'candidate_missing'):
            await apply_retention(self.target, manifest)
        self.assertEqual(before, await self.all_quotes(self.target))
