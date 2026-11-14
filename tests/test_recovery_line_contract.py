"""Explicit winner no-line evidence survives recovery without rewriting history."""
import copy
from decimal import Decimal
import os
import unittest

import asyncpg

from infra.recovery.bundle import audit_inventory, restore_bundle
from infra.recovery.legacy_quality import classify, plan
import test_recovery as support


class LegacyRecoveryLineTests(unittest.TestCase):
    def test_explicit_winner_null_and_legacy_zero_match_without_mutation(self):
        for line,quote_line in ((None,None),(None,'0'),('0',None),('0','0')):
            with self.subTest(line=line,quote_line=quote_line):
                record=support.legacy(line=line)
                record['system_quote']['line']=quote_line
                original=copy.deepcopy(record)
                result=classify(record)
                self.assertEqual('complete',result['classification'],result)
                self.assertTrue(result['eligible_for_statistics'])
                self.assertEqual(original,record)
                self.assertEqual(line,result['preserved']['line'])
                self.assertEqual(quote_line,result['preserved']['system_quote']['line'])

    def test_numeric_null_unknown_kind_missing_key_and_wrong_evidence_stay_excluded(self):
        cases=[]
        for kind in ('handicap','total','unknown'):
            record=support.legacy(kind=kind,line=None)
            record['system_quote']['line']=None
            cases.append(record)
        for target in ('record','quote'):
            record=support.legacy(line=None)
            record['system_quote']['line']=None
            del (record if target=='record' else record['system_quote'])['line']
            cases.append(record)
        for changes in ({'kind':'total'}, {'source':'tg_original'}, {'evidence_kind':'original_odds'}, {'line':'.25'}):
            record=support.legacy(line=None)
            record['system_quote'].update({'line':None,**changes})
            cases.append(record)
        for record in cases:
            with self.subTest(record=record):
                original=copy.deepcopy(record)
                result=classify(record)
                self.assertEqual('insufficient_evidence',result['classification'])
                self.assertFalse(result['eligible_for_statistics'])
                self.assertEqual(original,record)

    def test_null_and_zero_winner_duplicate_is_quarantined_both_directions(self):
        old=support.legacy()
        current=support.legacy(legacy_id='new',line=None)
        current['system_quote']['line']=None
        report=plan([old,current])
        self.assertEqual(2,report['counts']['insufficient_evidence'])
        self.assertEqual(0,report['statistics_eligible'])
        self.assertTrue(all(row['reasons']==['duplicate_recommendation_identity'] for row in report['records']))


@unittest.skipUnless(os.environ.get('RECOVERY_TEST_ADMIN_URL'),'requires explicit task-owned recovery PostgreSQL')
class RecoveryLineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.RecoveryIntegrationTests.asyncSetUp
    asyncTearDown=support.RecoveryIntegrationTests.asyncTearDown
    populate=support.RecoveryIntegrationTests.populate
    bundle=support.RecoveryIntegrationTests.bundle

    async def evidence_snapshot(self,conn):
        return [dict(row) for row in await conn.fetch(
            'SELECT id,reference_id,line,price,evidence,captured_at FROM v2_quote_evidence ORDER BY id')]

    async def test_winner_null_backup_restores_zero_missing_evidence_and_original_records(self):
        await self.source.execute("UPDATE v2_expert_signals SET market_kind='winner',line=NULL")
        await self.source.execute("UPDATE v2_quote_evidence SET line=NULL,evidence=evidence || '{\"market_kind\":\"winner\"}'::jsonb")
        before=await self.evidence_snapshot(self.source)
        inventory=await audit_inventory(self.source)
        self.assertEqual(0,inventory['signals_without_quote_evidence'])
        stream,manifest=await self.bundle()
        report=await restore_bundle(self.target,stream,self.backup_key,support.MemoryObjects())
        self.assertEqual({},report['differences'])
        self.assertEqual(inventory,report['inventory_before'])
        self.assertEqual(inventory,report['inventory_after'])
        self.assertEqual(before,await self.evidence_snapshot(self.source))
        self.assertEqual(before,await self.evidence_snapshot(self.target))
        self.assertEqual(2,await self.target.fetchval('SELECT count(*) FROM v2_expert_signals WHERE line IS NULL'))
        self.assertEqual(manifest['inventory'],await audit_inventory(self.target))

    async def test_inventory_accepts_legacy_winner_zero_only_and_keeps_numeric_null_missing(self):
        await self.source.execute("UPDATE v2_expert_signals SET market_kind='winner',line=NULL")
        before=await self.evidence_snapshot(self.source)
        self.assertEqual(0,(await audit_inventory(self.source))['signals_without_quote_evidence'])
        self.assertEqual(before,await self.evidence_snapshot(self.source))
        # Deliberately corrupted rows exist only inside a rollback-only test
        # transaction: production CHECK constraints remain present and intact.
        transaction=self.source.transaction()
        await transaction.start()
        try:
            await self.source.execute('ALTER TABLE v2_expert_signals DROP CONSTRAINT v2_signals_explicit_line_check')
            await self.source.execute('ALTER TABLE v2_quote_evidence DROP CONSTRAINT v2_evidence_explicit_line_check')
            for kind in ('total','handicap','unknown'):
                await self.source.execute('UPDATE v2_expert_signals SET market_kind=$1,line=NULL',kind)
                await self.source.execute("UPDATE v2_quote_evidence SET line=NULL,evidence='{}'::jsonb")
                self.assertEqual(2,(await audit_inventory(self.source))['signals_without_quote_evidence'])
            await self.source.execute("UPDATE v2_expert_signals SET market_kind='winner',line=NULL")
            for kind in ('total','unknown'):
                await self.source.execute('UPDATE v2_quote_evidence SET evidence=$1',{'market_kind':kind})
                self.assertEqual(2,(await audit_inventory(self.source))['signals_without_quote_evidence'])
            await self.source.execute("UPDATE v2_quote_evidence SET evidence='{\"market_kind\":\"winner\"}'::jsonb")
            self.assertEqual(0,(await audit_inventory(self.source))['signals_without_quote_evidence'])
        finally:
            await transaction.rollback()
        self.assertEqual(before,await self.evidence_snapshot(self.source))
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.source.execute("UPDATE v2_expert_signals SET market_kind='total',line=NULL")
