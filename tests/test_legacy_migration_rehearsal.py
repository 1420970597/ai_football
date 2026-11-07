"""Legacy original evidence is staged only in an isolated temporary PostgreSQL."""
import copy
import hashlib
import os
import unittest
from unittest.mock import patch

import asyncpg

from infra.recovery.bundle import RecoveryError, audit_inventory
from infra.recovery.legacy_quality import classify, plan, rehearse_migration
import test_recovery as support


class LegacyVersionClassificationTests(unittest.TestCase):
    def test_author_rename_preserves_stable_id_and_ordered_message_revisions(self):
        first = support.legacy(revision=1, version_key=hashlib.sha256(b'v1').hexdigest(), author_nickname='旧昵称')
        second = support.legacy(legacy_id='revision-2', revision=2, version_key=hashlib.sha256(b'v2').hexdigest(),
                                author_nickname='新昵称', raw_text='修订后原文')
        before = copy.deepcopy([first,second])
        result = plan([first,second])
        self.assertEqual(2,result['counts']['complete'])
        self.assertEqual(1,result['statistics_eligible'])
        self.assertEqual(['superseded_original_message_version'],result['records'][0]['reasons'])
        self.assertEqual('123',result['records'][0]['preserved']['author_id'])
        self.assertEqual('123',result['records'][1]['preserved']['author_id'])
        self.assertEqual(['旧昵称','新昵称'],[row['preserved']['author_nickname'] for row in result['records']])
        self.assertEqual([1,2],[row['preserved']['revision'] for row in result['records']])
        self.assertEqual(before,[first,second])

    def test_conflicting_author_and_same_version_original_are_quarantined(self):
        for records, reason in (([support.legacy(),support.legacy(legacy_id='different-author',author_id='999')],
                                 'conflicting_original_message_author'),
                                ([support.legacy(revision=1),support.legacy(legacy_id='other',revision=1,
                                  raw_text='同版本不同原文',market_id='2')],
                                 'conflicting_original_message_version_content')):
            with self.subTest(reason=reason):
                result = plan(records)
                self.assertEqual(0,result['statistics_eligible'])
                self.assertTrue(all(row['reasons']==[reason] for row in result['records']))

    def test_unordered_hash_versions_and_invalid_revisions_do_not_count(self):
        records = [support.legacy(version_key=hashlib.sha256(b'a').hexdigest()),
                   support.legacy(legacy_id='b',version_key=hashlib.sha256(b'b').hexdigest())]
        result = plan(records)
        self.assertEqual(0,result['statistics_eligible'])
        self.assertTrue(all('original_message_versions_cannot_be_ordered' in row['reasons'] for row in result['records']))
        for revision in (0,-1,True,'2'):
            self.assertFalse(classify(support.legacy(revision=revision))['eligible_for_statistics'])

    def test_multi_recommendations_same_original_keep_each_market_and_group_separate(self):
        first = support.legacy(revision=1)
        second = copy.deepcopy(first)
        second.update(legacy_id='other-market',market_id='2')
        second['system_quote']['market_id']='2'
        third = support.legacy(legacy_id='other-group',chat_id='-1002',author_id='456')
        result = plan([first,second,third,support.legacy(origin_kind='model')])
        self.assertEqual(3,result['statistics_eligible'])
        self.assertEqual(1,result['counts']['excluded_nonexpert'])
        self.assertEqual('origin_is_not_proven_telegram_expert',result['records'][-1]['reasons'][0])


class LegacyMigrationGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonisolated_connection_rejected_before_business_or_temporary_write(self):
        class ProductionConnection:
            def __init__(self): self.calls=[]
            async def fetchrow(self, query):
                self.calls.append(query)
                return {'role':'platform_api','name':'production'}
        conn = ProductionConnection()
        with self.assertRaisesRegex(RecoveryError,'only_explicit_isolated'):
            await rehearse_migration(conn,[support.legacy()])
        self.assertEqual(1,len(conn.calls))
        self.assertIn('current_user',conn.calls[0])


@unittest.skipUnless(os.environ.get('RECOVERY_TEST_ADMIN_URL'),'requires explicitly isolated recovery PostgreSQL')
class LegacyMigrationPostgresTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.RecoveryIntegrationTests.asyncSetUp
    asyncTearDown = support.RecoveryIntegrationTests.asyncTearDown
    populate = support.RecoveryIntegrationTests.populate

    def records(self):
        full = support.legacy(revision=1,version_key=hashlib.sha256(b'first').hexdigest())
        pending = support.legacy(legacy_id='pending',platform_message_id='46',official_result={},settlement={})
        recoverable = support.legacy(legacy_id='recoverable',platform_message_id='47',system_quote={},
                                     evidence_locator='project-only-test-reference')
        missing = support.legacy(legacy_id='missing-author',platform_message_id='48',author_id=None)
        model = support.legacy(legacy_id='old-model',origin_kind='model',platform_message_id='49',
                               settlement={'outcome':'win','unit_net':'999999'})
        return [full,pending,recoverable,missing,model]

    async def test_pg_staging_preserves_full_original_classifies_and_keeps_financial_inventory(self):
        records = self.records()
        original = copy.deepcopy(records)
        before = await audit_inventory(self.target)
        report = await rehearse_migration(self.target,records)
        self.assertEqual({'complete':2,'recoverable':1,'insufficient_evidence':1,'excluded_nonexpert':1},report['classification_counts'])
        self.assertEqual(5,report['input_records'])
        self.assertEqual(report['source_summary'],report['staged_summary'])
        self.assertEqual(1,report['staged_summary']['statistics_eligible'])
        self.assertEqual('0.95',report['staged_summary']['verified_expert_unit_net'])
        self.assertNotIn('999999',report['staged_summary']['verified_expert_unit_net'])
        self.assertEqual({},report['staging_differences'])
        self.assertEqual({},report['business_differences'])
        self.assertEqual(before,await audit_inventory(self.target))
        self.assertFalse(report['import_performed'])
        self.assertFalse(report['deletion_performed'])
        self.assertEqual(original,records)
        self.assertIsNone(await self.target.fetchval("SELECT to_regclass('pg_temp.legacy_expert_rehearsal')"))

    async def test_populated_ledger_orders_unknown_currency_and_balances_unchanged(self):
        before = await audit_inventory(self.source)
        report = await rehearse_migration(self.source,self.records())
        self.assertEqual(before,report['business_before']['inventory'])
        self.assertEqual(before,report['business_after']['inventory'])
        self.assertEqual(report['business_before']['table_hashes'],report['business_after']['table_hashes'])
        currencies = report['business_after']['order_currencies']
        self.assertEqual('unknown',currencies[0]['currency'])
        self.assertEqual(2,currencies[0]['orders'])
        self.assertEqual('4.00',currencies[0]['requested_stake'])
        self.assertEqual('2.00',currencies[0]['reserved_stake'])
        self.assertEqual('1.90000',currencies[0]['reported_settled_net'])
        self.assertEqual(0,report['explained_differences']['real_orders_imported'])

    async def test_pg_revisions_keep_original_hashes_do_not_double_count_and_repeat_cleanly(self):
        first = support.legacy(revision=1,version_key=hashlib.sha256(b'v1').hexdigest())
        second = support.legacy(legacy_id='v2',revision=2,version_key=hashlib.sha256(b'v2').hexdigest(),raw_text='原文修订')
        records = [first,second]
        one = await rehearse_migration(self.target,records)
        two = await rehearse_migration(self.target,records)
        self.assertEqual(one,two)
        self.assertEqual(2,one['staged_summary']['message_versions'])
        self.assertEqual(1,one['staged_summary']['statistics_eligible'])
        self.assertEqual('0.95',one['staged_summary']['verified_expert_unit_net'])

    async def test_conflict_model_and_missing_evidence_report_exclusions_without_import(self):
        first = support.legacy()
        duplicate = support.legacy(legacy_id='duplicate')
        report = await rehearse_migration(self.target,[first,duplicate,support.legacy(origin_kind='shadow_model')])
        self.assertEqual(0,report['staged_summary']['statistics_eligible'])
        self.assertEqual(2,report['classification_counts']['insufficient_evidence'])
        self.assertEqual(1,report['explained_differences']['nonexpert_records_not_imported'])
        self.assertEqual(2,report['explained_differences']['unverified_expert_records_excluded_from_statistics'])
        self.assertEqual(0,await self.target.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_pg_corrupt_staged_original_rolls_back_and_leaves_business_unchanged(self):
        before = await audit_inventory(self.target)
        original = asyncpg.Connection.execute
        async def corrupt(conn, query, *args, **kwargs):
            result = await original(conn,query,*args,**kwargs)
            if query.startswith('INSERT INTO pg_temp.legacy_expert_rehearsal'):
                await original(conn,"UPDATE pg_temp.legacy_expert_rehearsal SET source_record=source_record || '{\"raw_text\":\"篡改\"}'::jsonb")
            return result
        with patch.object(asyncpg.Connection,'execute',corrupt):
            with self.assertRaisesRegex(RecoveryError,'legacy_original_evidence_differs'):
                await rehearse_migration(self.target,[support.legacy()])
        self.assertEqual(before,await audit_inventory(self.target))
        self.assertIsNone(await self.target.fetchval("SELECT to_regclass('pg_temp.legacy_expert_rehearsal')"))

    async def test_pg_corrupt_classification_or_profit_rejected_not_reconciled_with_zero(self):
        original = asyncpg.Connection.execute
        async def corrupt(conn, query, *args, **kwargs):
            result = await original(conn,query,*args,**kwargs)
            if query.startswith('INSERT INTO pg_temp.legacy_expert_rehearsal'):
                await original(conn,'UPDATE pg_temp.legacy_expert_rehearsal SET verified_net=999')
            return result
        with patch.object(asyncpg.Connection,'execute',corrupt):
            with self.assertRaisesRegex(RecoveryError,'legacy_staging_inventory_differs'):
                await rehearse_migration(self.target,[support.legacy()])
        self.assertEqual(0,await self.target.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_budget_rejection_and_nonjson_input_do_not_create_stage(self):
        for records in ([None], [{'bad':object()}], [support.legacy()]*10001):
            with self.assertRaises(RecoveryError):
                await rehearse_migration(self.target,records)
            self.assertIsNone(await self.target.fetchval("SELECT to_regclass('pg_temp.legacy_expert_rehearsal')"))
