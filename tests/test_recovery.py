import copy
import base64
import asyncio
from datetime import datetime,timezone
from decimal import Decimal
import hashlib
import io
import json
import os
import re
from pathlib import Path
import unittest
from urllib.parse import urlsplit,urlunsplit
import uuid
import zipfile

import asyncpg

from infra.recovery.bundle import RecoveryError,audit_inventory,create_bundle,restore_bundle,validate_isolated_dsn
from infra.recovery.cli import IsolatedObjectDirectory,ROOT,project_path
from infra.recovery.legacy_quality import classify,plan
from services.api.app.database import apply_migrations,check_schema
from services.api.app.security import decrypt_secret,encrypt_secret,token_hash

DSN=os.environ.get('RECOVERY_TEST_ADMIN_URL','')


async def drop_owned_test_database(admin,name):
    if not re.fullmatch(r'recovery_[a-f0-9]{32}_(source|target)_test',name):
        raise RuntimeError('only_owned_recovery_test_cleanup_allowed')
    owned=await admin.fetchval('SELECT datdba=(SELECT oid FROM pg_roles WHERE rolname=current_user) FROM pg_database WHERE datname=$1',name)
    if owned is not True:
        raise RuntimeError('only_owned_recovery_test_cleanup_allowed')
    # All case-owned connections are closed first. FORCE additionally signals
    # other-role/background sessions and can fail for a legitimate CREATEDB
    # role. Stop new connections and allow closing backends to leave naturally;
    # never grant pg_signal_backend or kill another role to clean a test.
    await admin.execute('ALTER DATABASE "'+name+'" ALLOW_CONNECTIONS false')
    for attempt in range(20):
        try:
            await admin.execute('DROP DATABASE "'+name+'"')
            return
        except asyncpg.ObjectInUseError:
            if attempt==19:
                raise
            await asyncio.sleep(.25)


class MemoryObjects:
    def __init__(self,items=None):
        self.items=dict(items or {})

    async def get(self,key):
        return self.items[key]

    async def put(self,key,body,mime):
        if key in self.items:
            raise RuntimeError('object already exists')
        self.items[key]=body


def legacy(**updates):
    return {'legacy_id':'legacy-1','origin_kind':'telegram_expert','chat_id':'-1001','author_id':'123','platform_message_id':'45',
            'raw_text':'主队全场胜','posted_at':'2026-10-20T12:00:00Z','recommended_at':'2026-10-20T12:00:02Z',
            'fixture_id':'leyu:1','market_id':'1','period':'full_time','line':'0','selection':'home','kind':'winner','phase':'scheduled',
            'system_quote':{'source':'leyu','evidence_kind':'system_captured','price':'1.95','source_updated_at':'2026-10-20T12:00:01Z',
                            'fixture_id':'leyu:1','market_id':'1','period':'full_time','line':'0','selection':'home'},
            'official_result':{'fixture_id':'leyu:1','status':'confirmed','provider_reference':'official-1','content_hash':'a'*64,'source_updated_at':'2026-10-20T14:00:00Z','home':2,'away':1},
            'settlement':{'outcome':'win','unit_net':'.95','result_reference':'official-1'},**updates}


class LegacyQualityTests(unittest.TestCase):
    def test_only_reproducible_official_expert_record_is_statistics_eligible(self):
        result=classify(legacy())
        self.assertEqual('complete',result['classification'])
        self.assertTrue(result['eligible_for_statistics'])
        self.assertEqual('123',result['preserved']['author_id'])
        for changes in ({'official_result':{}},{'settlement':{'outcome':'loss','unit_net':'-1','result_reference':'official-1'}},
                        {'phase':'live','score_basis':'unknown'}):
            self.assertFalse(classify(legacy(**changes))['eligible_for_statistics'])
        wrong=legacy()
        wrong['system_quote']['fixture_id']='leyu:other'
        self.assertFalse(classify(wrong)['eligible_for_statistics'])

    def test_original_odds_missing_source_and_missing_author_never_fabricated(self):
        result=classify(legacy(system_quote={'source':'tg_original','price':'99'}))
        self.assertEqual('insufficient_evidence',result['classification'])
        self.assertFalse(result['eligible_for_statistics'])
        self.assertEqual('insufficient_evidence',classify(legacy(author_id=None))['classification'])
        recoverable=classify(legacy(system_quote={},evidence_locator='project-sample-reference'))
        self.assertEqual('recoverable',recoverable['classification'])

    def test_old_model_and_ambiguous_duplicates_excluded(self):
        rows=[legacy(origin_kind='model'),legacy(),legacy(legacy_id='other')]
        report=plan(rows)
        self.assertEqual({'complete':0,'recoverable':0,'insufficient_evidence':2,'excluded_nonexpert':1},report['counts'])
        self.assertFalse(report['import_performed'])
        self.assertFalse(report['deletion_performed'])

    def test_duplicate_identity_normalizes_line_and_ignores_nonexpert_record(self):
        distinct=plan([legacy(origin_kind='model'),legacy()])
        self.assertEqual(1,distinct['statistics_eligible'])
        conflict=plan([legacy(),legacy(legacy_id='another',line='0.00')])
        self.assertEqual(2,conflict['counts']['insufficient_evidence'])
        with self.assertRaises(ValueError):
            plan([None])

    def test_cli_paths_and_object_traversal_stay_in_project(self):
        root=ROOT/'.runtime/recovery-tests'
        self.assertEqual(root,project_path(root))
        objects=IsolatedObjectDirectory(root)
        self.assertEqual(root/'media/image',objects.key_path('media/image'))
        for key in ('../../../../outside','/outside'):
            with self.assertRaises(RecoveryError):
                objects.key_path(key)
        with self.assertRaises(RecoveryError):
            project_path(ROOT.parent/'outside')

    def test_production_database_rejected_before_connection(self):
        for dsn in ('postgresql://production:secret@remote/production','postgresql://recovery_test:secret@ai-football-v2-pgtest/production'):
            with self.assertRaises(RecoveryError):
                validate_isolated_dsn(dsn)
        validate_isolated_dsn('postgresql://recovery_test:test-only@ai-football-v2-pgtest/recovery_example_test')


@unittest.skipUnless(DSN,'需要显式隔离Docker建库角色，不使用生产连接')
class RecoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parts=urlsplit(DSN)
        if parts.hostname!='ai-football-v2-pgtest' or parts.username!='recovery_test' or parts.path!='/postgres':
            raise RuntimeError('恢复测试只允许本任务临时Docker角色')
        self.admin=await asyncpg.connect(DSN)
        self.names=['recovery_'+uuid.uuid4().hex+'_source_test','recovery_'+uuid.uuid4().hex+'_target_test']
        self.connections=[]
        for name in self.names:
            await self.admin.execute('CREATE DATABASE '+name)
            dsn=urlunsplit((parts.scheme,parts.netloc,'/'+name,parts.query,''))
            if name==self.names[0]:
                self.source_dsn=dsn
            validate_isolated_dsn(dsn)
            conn=await asyncpg.connect(dsn)
            for type_name in ('json','jsonb'):
                await conn.set_type_codec(type_name,schema='pg_catalog',encoder=json.dumps,decoder=json.loads)
            await apply_migrations(conn)
            self.connections.append(conn)
        self.source,self.target=self.connections
        self.data_key=b'd'*32
        self.backup_key=b'b'*32
        # 公开标准PNG编码的1×1隔离样本，不含真实群组图片。
        self.media_body=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aNuUAAAAASUVORK5CYII=')
        self.media_key='telegram/media/'+hashlib.sha256(self.media_body).hexdigest()
        self.objects=MemoryObjects({self.media_key:self.media_body})
        await self.populate()

    async def asyncTearDown(self):
        try:
            for conn in self.connections:
                await conn.close()
            for name in self.names:
                await drop_owned_test_database(self.admin,name)
        finally:
            await self.admin.close()

    async def populate(self):
        conn=self.source
        user,admin,source,expert,message,account=[uuid.uuid4() for _ in range(6)]
        self.user=user
        for identity,role in ((user,'user'),(admin,'admin')):
            await conn.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,$2,$3,'isolated-hash',$3)",identity,f'{identity}@test.invalid',role)
        await conn.execute("INSERT INTO v2_sessions(token_hash,csrf_hash,user_id,session_kind,expires_at) VALUES($1,$2,$3,'user',now()+interval '1 day')",token_hash('isolated-session'),token_hash('isolated-csrf'),user)
        await conn.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'-100123','群组')",source)
        await conn.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'123456','专家昵称')",expert)
        await conn.execute('INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)',source,expert)
        media=[{'object_key':self.media_key,'mime_type':'image/png','sha256':hashlib.sha256(self.media_body).hexdigest(),'bytes':len(self.media_body)}]
        await conn.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at,media_objects) VALUES($1,$2,$3,'1','原文及图片',now(),$4)",message,source,expert,media)
        await conn.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase,source_updated_at) VALUES('leyu:1','leyu:1','football','联赛','主','客',now(),'finished',now())")
        await conn.execute("INSERT INTO v2_fixture_results(fixture_id,version,provider_reference,content_hash,status,home_score,away_score,source_updated_at,evidence) VALUES('leyu:1',1,'official','hash','confirmed',2,1,now(),$1)",{'source':'generated-test-only'})
        encrypted=encrypt_secret(self.data_key,json.dumps({'username':'isolated-owner','password':'isolated-private-password'}))
        session=encrypt_secret(self.data_key,json.dumps({'leyu_request_id':'isolated-venue-session'}))
        await conn.execute("INSERT INTO v2_leyu_accounts(id,user_id,encrypted_credentials,encrypted_session,account_label,health_state) VALUES($1,$2,$3,$4,'本人','healthy')",account,user,encrypted,session)
        for n in range(2):
            signal=uuid.uuid4()
            await conn.execute("INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,line,selection,recommendation_price,quote_time,confidence,outcome,unit_net,result_version) "
                "VALUES($1,$2,$3,'leyu:1','scheduled',$4,'独赢',0,'home',1.95,now(),0.9,$5,$6,$7)",signal,message,expert,str(n+1),'win' if n==0 else 'pending',Decimal('.95') if n==0 else None,1 if n==0 else None)
            await conn.execute("INSERT INTO v2_quote_evidence(id,fixture_id,market_id,market_name,line,selection,price,evidence_kind,reference_id,source_updated_at) VALUES($1,'leyu:1',$2,'独赢',0,'home',1.95,'expert_signal',$3,now())",uuid.uuid4(),str(n+1),signal)
            if n==0:
                await conn.execute("INSERT INTO v2_signal_settlements(signal_id,fixture_id,result_version,algorithm_version,outcome,unit_net,effective_stake,reason,calculation) VALUES($1,'leyu:1',1,'test','win',.95,1,'generated_test',$2)",signal,{})
            await conn.execute("INSERT INTO v2_bet_orders(id,user_id,signal_id,idempotency_key,state,stake,price,account_id,reserved_stake,settled_net,provider_order_no) VALUES($1,$2,$3,$4,$5,2,1.95,$6,$7,$8,$9)",
                uuid.uuid4(),user,signal,str(signal),'settled' if n==0 else 'unknown',account,Decimal('0') if n==0 else Decimal('2'),Decimal('1.90') if n==0 else None,'ORDER-'+str(n))
        await conn.execute("INSERT INTO v2_audit_log(id,actor_id,actor_role,action,resource_type,resource_id) VALUES($1,$2,'user','generated_recovery_test','account',$3)",uuid.uuid4(),user,str(account))

    async def bundle(self):
        stream=io.BytesIO()
        manifest=await create_bundle(self.source,stream,self.backup_key,self.objects,data_key_reference='isolated-data-key-v1')
        stream.seek(0)
        return stream,manifest

    async def test_full_database_media_key_and_financial_restore(self):
        stream,manifest=await self.bundle()
        target_objects=MemoryObjects()
        report=await restore_bundle(self.target,stream,self.backup_key,target_objects)
        self.assertEqual({},report['differences'])
        self.assertEqual(manifest['inventory'],await audit_inventory(self.target))
        self.assertEqual({'messages':1,'signals':2,'pending_signals':1,'settled_signals':1,'unit_net':'0.950000','orders':2,'order_stakes':'4.00','reserved_stakes':'2.00','actual_settled_net':'1.90000','signals_without_quote_evidence':0,'settled_signals_without_ledger':0,'audit_entries':1,'users':2},report['inventory_after'])
        await check_schema(self.target)
        restored=await self.target.fetchrow('SELECT encrypted_credentials,encrypted_session FROM v2_leyu_accounts')
        self.assertIn('isolated-private-password',decrypt_secret(self.data_key,restored['encrypted_credentials']))
        with self.assertRaises(Exception):
            decrypt_secret(b'wrong-key-is-never-the-data-key!!',restored['encrypted_credentials'])
        self.assertEqual(self.media_body,target_objects.items[self.media_key])
        self.assertEqual('user',await self.target.fetchval('SELECT session_kind FROM v2_sessions WHERE token_hash=$1',token_hash('isolated-session')))
        self.assertEqual('专家昵称',await self.target.fetchval('SELECT display_nickname FROM v2_experts'))
        self.assertGreater(report['table_count'],20)
        self.assertGreater(report['restore_elapsed_seconds'],0)
        print('RECOVERY_EVIDENCE '+json.dumps({'tables':report['table_count'],'backup_seconds':manifest['elapsed_seconds'],'restore_seconds':report['restore_elapsed_seconds'],
            'inventory_before':report['inventory_before'],'inventory_after':report['inventory_after'],'difference':report['differences'],'media_objects':report['media_objects']},sort_keys=True))

    async def test_pristine_seed_target_restores_nondefault_replay_budget_exactly(self):
        await self.source.execute("UPDATE v2_review_lane_schedule SET realtime_claims_since_replay=6,updated_at=clock_timestamp() WHERE singleton")
        expected=dict(await self.source.fetchrow('SELECT * FROM v2_review_lane_schedule'))
        target_seed=await self.target.fetchrow("SELECT seed.*,migration.applied_at FROM v2_review_lane_schedule seed "
            "JOIN v2_schema_migrations migration ON migration.version='017_review_freshness_lanes.sql'")
        self.assertEqual(0,target_seed['realtime_claims_since_replay'])
        self.assertEqual(target_seed['applied_at'],target_seed['updated_at'])
        stream,_=await self.bundle()
        report=await restore_bundle(self.target,stream,self.backup_key,MemoryObjects())
        self.assertEqual({},report['differences'])
        self.assertEqual(expected,dict(await self.target.fetchrow('SELECT * FROM v2_review_lane_schedule')))
        self.assertEqual(1,await self.target.fetchval('SELECT count(*) FROM v2_review_lane_schedule'))

    async def test_used_or_reset_seed_target_is_business_data_and_never_overwritten(self):
        stream,_=await self.bundle()
        for counter in (3,0):
            with self.subTest(counter=counter):
                await self.target.execute("UPDATE v2_review_lane_schedule SET realtime_claims_since_replay=$1,"
                    "updated_at=(SELECT applied_at+interval '1 second' FROM v2_schema_migrations "
                    "WHERE version='017_review_freshness_lanes.sql') WHERE singleton",counter)
                before=dict(await self.target.fetchrow('SELECT * FROM v2_review_lane_schedule'))
                objects=MemoryObjects()
                stream.seek(0)
                with self.assertRaisesRegex(RecoveryError,'restore_target_contains_business_data'):
                    await restore_bundle(self.target,stream,self.backup_key,objects)
                self.assertEqual(before,dict(await self.target.fetchrow('SELECT * FROM v2_review_lane_schedule')))
                self.assertEqual({},objects.items)
                self.assertEqual(0,await self.target.fetchval('SELECT count(*) FROM v2_users'))

    async def test_backup_is_encrypted_wrong_key_and_corrupt_chunk_rejected(self):
        stream,manifest=await self.bundle()
        raw=stream.getvalue()
        self.assertNotIn(b'isolated-private-password',raw)
        self.assertNotIn('原文及图片'.encode(),raw)
        with self.assertRaises(RecoveryError):
            await restore_bundle(self.target,io.BytesIO(raw),b'x'*32,MemoryObjects())
        damaged=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as source,zipfile.ZipFile(damaged,'w') as target:
            selected=manifest['tables']['v2_expert_messages']['chunks'][0]
            for item in source.infolist():
                data=source.read(item)
                if item.filename==selected:
                    data=data[:-1]+bytes([data[-1]^1])
                target.writestr(item.filename,data)
        damaged.seek(0)
        with self.assertRaises(Exception):
            await restore_bundle(self.target,damaged,self.backup_key,MemoryObjects())
        self.assertEqual(0,await self.target.fetchval('SELECT count(*) FROM v2_users'))
        await check_schema(self.target)

    async def test_missing_or_wrong_media_fails_backup_no_false_success(self):
        for objects in (MemoryObjects(),MemoryObjects({self.media_key:b'wrong-image'})):
            with self.assertRaises(RecoveryError):
                await create_bundle(self.source,io.BytesIO(),self.backup_key,objects,data_key_reference='isolated-data-key-v1')
        self.assertEqual(2,await self.source.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_restore_refuses_business_target_and_media_conflict(self):
        stream,_=await self.bundle()
        await self.target.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,'existing@test.invalid','existing','hash','user')",uuid.uuid4())
        objects=MemoryObjects()
        with self.assertRaises(RecoveryError):
            await restore_bundle(self.target,stream,self.backup_key,objects)
        self.assertEqual({},objects.items)
        self.assertEqual(1,await self.target.fetchval('SELECT count(*) FROM v2_users'))
        await self.target.execute('DELETE FROM v2_users')
        stream.seek(0)
        conflict=MemoryObjects({self.media_key:b'existing-different-content'})
        with self.assertRaises(RecoveryError):
            await restore_bundle(self.target,stream,self.backup_key,conflict)
        self.assertEqual(b'existing-different-content',conflict.items[self.media_key])

    async def test_repeatable_read_snapshot_does_not_mix_later_commit(self):
        original=self.source
        source_dsn=self.source_dsn

        class ConcurrentWriter:
            inserted=False

            def __getattr__(self,name):
                return getattr(original,name)

            async def copy_from_table(self,*args,**kwargs):
                # The snapshot/inventory already exists, but no table has been
                # copied yet. READ COMMITTED would mix the new audit row in.
                if not self.inserted:
                    self.inserted=True
                    writer=await asyncpg.connect(source_dsn)
                    try:
                        await writer.execute("INSERT INTO v2_audit_log(id,actor_role,action,resource_type) VALUES($1,'test','committed_after_snapshot','recovery')",uuid.uuid4())
                    finally:
                        await writer.close()
                return await original.copy_from_table(*args,**kwargs)

        stream=io.BytesIO()
        manifest=await create_bundle(ConcurrentWriter(),stream,self.backup_key,self.objects,data_key_reference='isolated-data-key-v1')
        stream.seek(0)
        self.assertEqual(1,manifest['inventory']['audit_entries'])
        self.assertEqual(2,await self.source.fetchval('SELECT count(*) FROM v2_audit_log'))
        restored=await restore_bundle(self.target,stream,self.backup_key,MemoryObjects())
        self.assertEqual(1,restored['inventory_after']['audit_entries'])
        self.assertEqual({},restored['differences'])

    async def test_schema_mismatch_rejected_before_objects_or_business_write(self):
        stream,_=await self.bundle()
        await self.target.execute('ALTER TABLE v2_experts ADD COLUMN incompatible_test text')
        objects=MemoryObjects()
        with self.assertRaisesRegex(RecoveryError,'restore_schema_differs'):
            await restore_bundle(self.target,stream,self.backup_key,objects)
        self.assertEqual({},objects.items)
        self.assertEqual(0,await self.target.fetchval('SELECT count(*) FROM v2_users'))


if __name__=='__main__':
    unittest.main()
