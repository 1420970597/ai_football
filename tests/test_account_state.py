"""每例独立临时库，所有账户与资金协议使用 Fake HTTP；拒绝生产连接。"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit, urlunsplit
import uuid

import asyncpg
from fastapi import FastAPI
import httpx

from services.api.app import account_jobs, database, orders
from services.api.app.routes import accounts
from services.api.app.security import decrypt_secret, encrypt_secret, token_hash
from services.executor.account_protocol import AccountProtocolClient, BET_PATH, AMOUNT_PATH, LATEST_MARKET_PATH, LIMITS_PATH, ORDERS_PATH
from services.executor.worker import Executor
from services.executor.account_session import AccountScope,AccountSessionManager,RenewalHooks


DSN = os.environ.get('ACCOUNT_TEST_DATABASE_URL','')


@unittest.skipUnless(DSN,'需显式本任务临时Docker数据库，禁止生产默认值')
class AccountStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parts=urlsplit(DSN)
        if parts.hostname!='ai-football-v2-pgtest' or parts.username!='account_state_test' or parts.path!='/postgres':
            raise RuntimeError('账户测试只允许本任务Docker专用建库角色')
        self.dbname='account_state_'+uuid.uuid4().hex+'_test'
        admin=await asyncpg.connect(DSN)
        try:
            await admin.execute('CREATE DATABASE '+self.dbname)
        finally:
            await admin.close()
        async def init(conn):
            for type_name in ('json','jsonb'):
                await conn.set_type_codec(type_name,schema='pg_catalog',encoder=json.dumps,decoder=json.loads)
        task_dsn=urlunsplit((parts.scheme,parts.netloc,'/'+self.dbname,parts.query,''))
        self.pool=await asyncpg.create_pool(task_dsn,min_size=1,max_size=5,init=init)
        root=Path(__file__).resolve().parents[1]
        async with self.pool.acquire() as conn:
            for migration in sorted((root/'services/api/migrations').glob('[0-9]*_*.sql')):
                await conn.execute(migration.read_text())
        self.old_pool=database.pool
        database.pool=self.pool
        self.user={'id':uuid.uuid4(),'role':'user','session_kind':'user','reauthenticated_at':datetime.now(timezone.utc)}
        await self.pool.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,$2,'用户','hash','user')",self.user['id'],f"{self.user['id']}@test.invalid")
        await self.recent_session()
        self.settings=SimpleNamespace(encryption_key=b'k'*32,executor_token='isolated-executor-token',live_orders_enabled=False,quote_max_age_seconds=60)
        self.app=FastAPI()
        self.app.state.settings=self.settings
        self.app.include_router(accounts.router)
        self.app.include_router(accounts.executor_router)
        self.patch=patch.object(accounts,'require_user',AsyncMock(side_effect=lambda request:dict(self.user)))
        self.patch.start()
        self.api=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://platform.invalid')
        self.worker_api=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://platform.invalid',headers={'Authorization':'Bearer isolated-executor-token'})
        for key,value in {'leyu_app_host':'https://app.invalid','leyu_app_signing_config':'/run/secrets/no-live-signing.json',
                          'leyu_app_login_name':'DO-NOT-REUSE-COLLECTOR','leyu_app_token':'DO-NOT-REUSE-TOKEN'}.items():
            await self.pool.execute('INSERT INTO v2_admin_settings(setting_key,encrypted_value) VALUES($1,$2)',key,encrypt_secret(b'k'*32,value))
        self.native_requests=[]
        self.native_submission='accepted'

    async def asyncTearDown(self):
        self.patch.stop()
        await self.api.aclose()
        await self.worker_api.aclose()
        database.pool=self.old_pool
        await self.pool.close()
        admin=await asyncpg.connect(DSN)
        try:
            await admin.execute('DROP DATABASE '+self.dbname+' WITH (FORCE)')
        finally:
            await admin.close()

    async def recent_session(self):
        self.user['_token_hash'] = token_hash('isolated-current-session-'+str(self.user['id']))
        await self.pool.execute("INSERT INTO v2_sessions(token_hash,csrf_hash,user_id,session_kind,expires_at,reauthenticated_at) "
                                "VALUES($1,$2,$3,'user',now()+interval '1 day',now())",self.user['_token_hash'],token_hash('isolated-csrf'),self.user['id'])

    async def bind(self,username='own-user'):
        response=await self.api.put('/api/v1/accounts/leyu',json={'account_label':'本人账户','username':username,'password':'own-password'})
        self.assertEqual(200,response.status_code,response.text)
        return await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE user_id=$1',self.user['id'])

    async def healthy(self):
        account=await self.bind()
        session=encrypt_secret(b'k'*32,json.dumps({'leyu_api_base':'https://venue.invalid','leyu_origin':'https://ui.invalid','leyu_request_id':'own-business-session'}))
        await self.pool.execute("UPDATE v2_leyu_accounts SET encrypted_session=$2,health_state='healthy',last_verified_at=now(),available_balance=100,balance_observed_at=now() WHERE id=$1",account['id'],session)
        await self.pool.execute("UPDATE v2_account_jobs SET state='succeeded' WHERE account_id=$1",account['id'])
        return await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE id=$1',account['id'])

    async def signal(self):
        source,expert,message,signal,quote=[uuid.uuid4() for _ in range(5)]
        fixture='leyu:'+uuid.uuid4().hex
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,$2,'群')",source,str(source))
        await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,$2,'专家昵称')",expert,str(expert))
        await self.pool.execute('INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)',source,expert)
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,'1','大2.5',now())",message,source,expert)
        await self.pool.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase,source_updated_at) VALUES($1,$1,'football','联赛','甲','乙',now(),'live',now())",fixture)
        await self.pool.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,line,selection,price,source_updated_at,period,market_kind,status,play_id,market_instance_id,selection_id) "
                                "VALUES($1,$2,'2','全场大小',2.5,'over',1.95,now(),'full_time','total','active','2','market','option')",quote,fixture)
        await self.pool.execute("INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,line,selection,recommendation_price,quote_time,confidence,period,market_kind,score_baseline) "
                                "VALUES($1,$2,$3,$4,'live','2','全场大小',2.5,'over',1.95,now(),0.9,'full_time','total',$5)",signal,message,expert,fixture,{'home_score':0,'away_score':0})
        return signal,expert,message,quote

    async def enable(self,experts,daily='4',exposure='4'):
        self.settings.live_orders_enabled=True
        await self.pool.execute("INSERT INTO v2_admin_settings(setting_key,encrypted_value) VALUES('auto_betting_enabled',$1) ON CONFLICT(setting_key) DO UPDATE SET encrypted_value=EXCLUDED.encrypted_value",encrypt_secret(b'k'*32,'true'))
        plan=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_membership_plans(id,code,name,price,duration_days,enabled,entitlements) VALUES($1,$2,'VIP',10,30,TRUE,$3)",plan,str(plan),{'automatic_betting':True})
        await self.pool.execute("INSERT INTO v2_memberships(id,user_id,plan_id,status,starts_at,ends_at,grant_source) VALUES($1,$2,$3,'active',now()-interval '1 minute',now()+interval '1 day','test')",uuid.uuid4(),self.user['id'],plan)
        response=await self.api.put('/api/v1/accounts/leyu/auto-betting',json={'enabled':True,'confirmation_phrase':'I_UNDERSTAND','allowed_experts':[str(value) for value in experts],
            'stake_per_order':'2','max_per_order':'2','daily_limit':daily,'open_exposure_limit':exposure})
        self.assertEqual(200,response.status_code,response.text)

    async def reserve(self,signal):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                return await orders.reserve_signal(conn,self.user['id'],signal,self.settings)

    def native(self,request):
        self.native_requests.append(request)
        self.assertEqual('own-business-session',request.headers['requestId'])
        if request.url.path==AMOUNT_PATH:
            value={'amount':'100'}
        elif request.url.path==LATEST_MARKET_PATH:
            query=json.loads(request.content)['idList'][0]
            value=[{'id':'market','matchInfoId':query['matchInfoId'],'playId':'2','matchStatus':1,'matchOver':0,'matchHandicapStatus':0,'status':0,'marketValue':'2.5',
                    'marketOddsList':[{'id':'option','oddsType':'Over','oddsStatus':1,'oddsValue':'195000'}]}]
        elif request.url.path==LIMITS_PATH:
            value=[{'code':0,'playOptionsId':'option','minBet':'1','orderMaxPay':'10'}]
        elif request.url.path==ORDERS_PATH:
            value={'records':[],'total':0}
        elif request.url.path==BET_PATH:
            if self.native_submission=='unknown':
                raise httpx.ReadTimeout('private-error')
            return httpx.Response(200,json={'code':0,'data':{'orderDetailRespList':[{'orderNo':'PROVIDER-ORDER','orderStatusCode':1}]}})
        else:
            raise AssertionError('unexpected path')
        return httpx.Response(200,json=value if request.url.path==AMOUNT_PATH else {'code':0,'data':value})

    def executor(self):
        return Executor(self.worker_api,'https://platform.invalid/internal/executor',live_orders_enabled=True,
                        client_factory=lambda scope,session,**kwargs:AccountProtocolClient(scope,session,transport=httpx.MockTransport(self.native),**kwargs))

    async def test_binding_encrypted_async_verify_no_collector_identity(self):
        account=await self.bind()
        self.assertNotIn('own-password',account['encrypted_credentials'])
        self.assertEqual('unverified',account['health_state'])
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        self.assertEqual('account',work['kind'])
        self.assertEqual('own-user',work['config']['leyu_app_login_name'])
        self.assertNotIn('leyu_app_token',work['config'])
        self.assertNotIn('DO-NOT-REUSE',json.dumps(work))
        visible=(await self.api.get('/api/v1/accounts/leyu')).json()
        self.assertNotIn('own-password',json.dumps(visible))
        self.assertFalse(visible['execution_capability']['live_enabled'])

    async def test_private_session_cas_and_renewal_mutex(self):
        account=await self.bind()
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        prefix=f"/internal/executor/jobs/{work['job_id']}"
        body={'attempt_id':work['attempt_id'],'expected_revision':1,'identity_hash':hashlib.sha256(b'2001:own-user').hexdigest()}
        first=await self.worker_api.post(prefix+'/renewal/claim',json=body)
        self.assertEqual(200,first.status_code,first.text)
        self.assertEqual(409,(await self.worker_api.post(prefix+'/renewal/claim',json=body)).status_code)
        updates={'leyu_app_token':'own-new-token','leyu_request_id':'own-session','leyu_api_base':'https://venue.invalid','leyu_origin':'https://ui.invalid'}
        persist={**{'attempt_id':work['attempt_id'],'renewal_attempt':first.json()['attempt_id'],'expected_revision':1},'updates':updates}
        result=await self.worker_api.put(prefix+'/session',json=persist)
        self.assertEqual(200,result.status_code,result.text)
        self.assertEqual(2,result.json()['revision'])
        self.assertEqual(409,(await self.worker_api.put(prefix+'/session',json=persist)).status_code)
        stored=await self.pool.fetchval('SELECT encrypted_session FROM v2_leyu_accounts WHERE id=$1',account['id'])
        self.assertNotIn('own-new-token',stored)
        self.assertEqual('own-new-token',json.loads(decrypt_secret(b'k'*32,stored))['leyu_app_token'])
        finish=await self.worker_api.post(prefix+'/renewal/finish',json={'attempt_id':work['attempt_id'],'renewal_attempt':first.json()['attempt_id'],'status':'unknown','metadata':{'login_submitted':True}})
        self.assertEqual(200,finish.status_code,finish.text)
        repeated=await self.worker_api.post(prefix+'/renewal/finish',json={'attempt_id':work['attempt_id'],'renewal_attempt':first.json()['attempt_id'],'status':'unknown','metadata':{'login_submitted':True}})
        self.assertEqual(200,repeated.status_code,repeated.text)
        self.assertTrue(repeated.json()['idempotent'])
        body['expected_revision']=2
        self.assertEqual(409,(await self.worker_api.post(prefix+'/renewal/claim',json=body)).status_code)

    async def test_account_worker_balance_without_funds_marks_verified(self):
        await self.healthy()
        await self.api.post('/api/v1/accounts/leyu/refresh')
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        await self.executor().account_job(work)
        account=await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE user_id=$1',self.user['id'])
        self.assertEqual('healthy',account['health_state'])
        self.assertEqual(Decimal('100'),account['available_balance'])
        self.assertNotIn(BET_PATH,[request.url.path for request in self.native_requests])

    async def test_actual_shared_renewal_mock_login_persists_user_session(self):
        await self.bind()
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        calls=[]

        def app_http(request):
            calls.append(request)
            if request.url.path.endswith('/preInfo'):
                return httpx.Response(200,json={'status_code':6000,'data':{'ip':'1.1.1.1'}})
            if request.url.path.endswith('/login'):
                body=json.loads(request.content)
                self.assertEqual('own-user',body['name'])
                self.assertEqual(hashlib.md5(b'own-password').hexdigest(),body['password'])
                return httpx.Response(200,json={'status_code':6000,'data':{'token':'private-own-app-token'}})
            self.assertTrue(request.url.path.endswith('/launch'))
            self.assertEqual('private-own-app-token',request.headers['x-api-token'])
            self.assertEqual('false',json.loads(request.content)['sendMoney'])
            return httpx.Response(200,json={'status_code':6000,'data':{'url':'https://venue.invalid/index?token=own-business-session','h5Url':'https://ui.invalid'}})

        executor=self.executor()
        executor.session_factory=lambda scope,config,hooks:AccountSessionManager(scope,config,hooks,direct_transport=httpx.MockTransport(app_http),verify_public_hosts=False)
        document={'routes':{name:{'key':'k'*16,'iv':'i'*16} for name in ('','/site/api','/game/api')}}
        with patch('services.collector.leyu_session.private_document',return_value=document):
            await executor.account_job(work)
        account=await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE user_id=$1',self.user['id'])
        self.assertEqual(('healthy',3,'ready'),(account['health_state'],account['config_revision'],account['renewal_state']))
        private=json.loads(decrypt_secret(b'k'*32,account['encrypted_session']))
        self.assertEqual('private-own-app-token',private['leyu_app_token'])
        self.assertEqual('own-business-session',private['leyu_request_id'])
        self.assertEqual(1,sum(request.url.path.endswith('/login') for request in calls))
        self.assertNotIn(BET_PATH,[request.url.path for request in self.native_requests])

    async def test_reservation_concurrent_idempotent_limits(self):
        await self.healthy()
        s1,e1,_,_=await self.signal()
        s2,e2,_,_=await self.signal()
        await self.enable([e1,e2],daily='2',exposure='2')
        duplicates=await asyncio.gather(*(self.reserve(s1) for _ in range(3)))
        self.assertEqual(1,len({row['id'] for row in duplicates}))
        with self.assertRaises(Exception):
            await self.reserve(s2)
        self.assertEqual(Decimal('2'),await self.pool.fetchval('SELECT sum(reserved_stake) FROM v2_bet_orders'))

    async def test_signal_source_revision_stale_and_membership_rechecked(self):
        await self.healthy()
        signal,expert,message,quote=await self.signal()
        await self.enable([expert])
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '2 minutes' WHERE id=$1",quote)
        with self.assertRaises(Exception):
            await self.reserve(signal)
        await self.pool.execute('UPDATE v2_current_quotes SET source_updated_at=now() WHERE id=$1',quote)
        await self.pool.execute('UPDATE v2_source_experts SET enabled=FALSE')
        with self.assertRaises(Exception):
            await self.reserve(signal)
        await self.pool.execute('UPDATE v2_source_experts SET enabled=TRUE')
        await self.pool.execute("UPDATE v2_memberships SET ends_at=now()-interval '1 second'")
        with self.assertRaises(Exception):
            await self.reserve(signal)
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_expiry_suspends_permission_and_renewal_does_not_silently_rearm(self):
        await self.healthy()
        signal, expert, _, _ = await self.signal()
        await self.enable([expert])
        order = await self.reserve(signal)
        await self.pool.execute("UPDATE v2_memberships SET ends_at=now()-interval '1 second'")
        response = await self.worker_api.post('/internal/executor/dispatch')
        self.assertEqual(200, response.status_code, response.text)
        policy = await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1', self.user['id'])
        self.assertFalse(policy['auto_enabled'])
        self.assertTrue(policy['emergency_stopped'])
        self.assertEqual('cancelled', await self.pool.fetchval('SELECT state FROM v2_bet_orders WHERE id=$1', order['id']))
        await self.pool.execute("UPDATE v2_memberships SET ends_at=now()+interval '1 day'")
        await self.worker_api.post('/internal/executor/dispatch')
        self.assertFalse(await self.pool.fetchval('SELECT auto_enabled FROM v2_user_bet_settings WHERE user_id=$1', self.user['id']))

    async def test_withdrawn_recommendation_cannot_reserve_money(self):
        await self.healthy()
        signal, expert, _, _ = await self.signal()
        await self.enable([expert])
        await self.pool.execute('UPDATE v2_expert_signals SET withdrawn_at=now() WHERE id=$1', signal)
        with self.assertRaises(Exception):
            await self.reserve(signal)

    async def test_mock_order_full_flow_and_owner_only_orders(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        row=await self.reserve(signal)
        work=(await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        self.assertEqual('order',work['kind'])
        await self.executor().order_job(work)
        stored=await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',row['id'])
        self.assertEqual('accepted',stored['state'])
        self.assertEqual('PROVIDER-ORDER',stored['provider_order_no'])
        self.assertEqual(Decimal('2'),stored['reserved_stake'])
        self.assertEqual(1,sum(request.url.path==BET_PATH for request in self.native_requests))
        listing=(await self.api.get('/api/v1/accounts/orders')).json()
        self.assertEqual(str(row['id']),listing['items'][0]['id'])
        other=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,$2,'另一用户','hash','user')",other,f'{other}@test.invalid')
        self.user['id']=other
        await self.recent_session()
        self.assertEqual([],(await self.api.get('/api/v1/accounts/orders')).json()['items'])

    async def test_unknown_mock_submission_not_requeued_or_released(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        order=await self.reserve(signal)
        work=(await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        self.native_submission='unknown'
        await self.executor().order_job(work)
        stored=await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',order['id'])
        self.assertEqual('unknown',stored['state'])
        self.assertEqual(Decimal('2'),stored['reserved_stake'])
        self.assertEqual('idle',(await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()['kind'])
        self.assertEqual(1,sum(request.url.path==BET_PATH for request in self.native_requests))

    async def test_executor_cancel_during_mock_send_preserves_durable_exposure(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        row=await self.reserve(signal)
        work=(await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        sent=asyncio.Event()

        async def transport(request):
            if request.url.path==BET_PATH:
                sent.set()
                await asyncio.Event().wait()
            return self.native(request)

        executor=self.executor()
        executor.client_factory=lambda scope,session,**kwargs:AccountProtocolClient(scope,session,transport=httpx.MockTransport(transport),**kwargs)
        task=asyncio.create_task(executor.order_job(work))
        await asyncio.wait_for(sent.wait(),2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        pending=await self.pool.fetchrow('SELECT state,reserved_stake FROM v2_bet_orders WHERE id=$1',row['id'])
        self.assertEqual(('submitting',Decimal('2')),(pending['state'],pending['reserved_stake']))
        await self.pool.execute("UPDATE v2_bet_orders SET lease_until=now()-interval '1 second' WHERE id=$1",row['id'])
        await self.worker_api.post('/internal/executor/claim?live_capable=true')
        pending=await self.pool.fetchrow('SELECT state,reserved_stake FROM v2_bet_orders WHERE id=$1',row['id'])
        self.assertEqual(('unknown',Decimal('2')),(pending['state'],pending['reserved_stake']))

    async def test_emergency_stop_after_claim_cancels_before_send(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        row=await self.reserve(signal)
        work=(await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        await self.api.post('/api/v1/accounts/leyu/emergency-stop')
        await self.executor().order_job(work)
        stored=await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',row['id'])
        self.assertEqual(('cancelled',Decimal('0')),(stored['state'],stored['reserved_stake']))
        self.assertNotIn(BET_PATH,[request.url.path for request in self.native_requests])

    async def test_rebinding_revokes_permit_and_preserves_sent_exposure(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        row=await self.reserve(signal)
        await self.pool.execute("UPDATE v2_bet_orders SET state='unknown' WHERE id=$1",row['id'])
        account=await self.bind('different-own-user')
        policy=await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1',self.user['id'])
        self.assertFalse(policy['auto_enabled'])
        self.assertIsNone(policy['consent_account_id'])
        self.assertEqual(2,account['binding_revision'])
        self.assertIsNone(account['encrypted_session'])
        self.assertEqual(Decimal('2'),await self.pool.fetchval('SELECT reserved_stake FROM v2_bet_orders WHERE id=$1',row['id']))

    async def test_expired_sending_lease_keeps_unknown_exposure(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        row=await self.reserve(signal)
        await self.pool.execute("UPDATE v2_bet_orders SET state='submitting',lease_until=now()-interval '1 second' WHERE id=$1",row['id'])
        await self.worker_api.post('/internal/executor/claim?live_capable=true')
        stored=await self.pool.fetchrow('SELECT state,reserved_stake FROM v2_bet_orders WHERE id=$1',row['id'])
        self.assertEqual(('unknown',Decimal('2')),(stored['state'],stored['reserved_stake']))

    async def test_active_funding_request_prevents_identity_switch_until_unknown(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        row=await self.reserve(signal)
        await self.pool.execute("UPDATE v2_bet_orders SET state='submitting',lease_until=now()+interval '30 seconds' WHERE id=$1",row['id'])
        response=await self.api.put('/api/v1/accounts/leyu',json={'account_label':'新账户','username':'new-user','password':'new-password'})
        self.assertEqual(409,response.status_code)
        self.assertEqual(409,(await self.api.delete('/api/v1/accounts/leyu')).status_code)
        await self.pool.execute("UPDATE v2_bet_orders SET lease_until=now()-interval '1 second' WHERE id=$1",row['id'])
        account=await self.bind('new-user')
        self.assertEqual(2,account['binding_revision'])
        preserved=await self.pool.fetchrow('SELECT state,reserved_stake FROM v2_bet_orders WHERE id=$1',row['id'])
        self.assertEqual(('unknown',Decimal('2')),(preserved['state'],preserved['reserved_stake']))

    async def test_live_default_false_and_service_auth_required(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        with self.assertRaises(Exception):
            await self.reserve(signal)
        self.assertEqual(401,(await self.api.post('/internal/executor/claim')).status_code)
        self.assertEqual(401,(await self.api.post('/internal/executor/dispatch')).status_code)
        self.assertEqual('idle',(await self.worker_api.post('/internal/executor/claim')).json()['kind'])

    async def test_account_rebinding_invalidates_running_renewal_cas(self):
        await self.bind()
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        root=f"/internal/executor/jobs/{work['job_id']}"
        claimed=await self.worker_api.post(root+'/renewal/claim',json={'attempt_id':work['attempt_id'],'expected_revision':1,'identity_hash':hashlib.sha256(b'2001:own-user').hexdigest()})
        await self.bind('another-identity')
        rejected=await self.worker_api.put(root+'/session',json={'attempt_id':work['attempt_id'],'expected_revision':1,'renewal_attempt':claimed.json()['attempt_id'],'updates':{'leyu_request_id':'stale-session'}})
        self.assertEqual(409,rejected.status_code)
        self.assertIsNone(await self.pool.fetchval('SELECT encrypted_session FROM v2_leyu_accounts WHERE user_id=$1',self.user['id']))

    async def test_api_authorization_rejects_rehashed_wrong_line_and_money(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        order=await self.reserve(signal)
        work=(await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        manager=AccountSessionManager(AccountScope(**work['scope']),work['config'],RenewalHooks(None,None,None))
        async with AccountProtocolClient(manager.scope,manager.venue_session(),transport=httpx.MockTransport(self.native)) as client:
            from services.executor.account_protocol import NativeSelection
            prepared=await client.preflight(NativeSelection(**work['selection']),Decimal(work['stake']),device_imei=work['config']['leyu_app_uuid'])
        for field,value in [('marketValue','3.5'),('betAmount','3.00'),('scoreBenchmark','9:9'),('playOptionsId','different')]:
            payload=prepared.payload
            payload['seriesOrders'][0]['orderDetailList'][0][field]=value
            encoded=json.dumps(payload,sort_keys=True,separators=(',',':'))
            prefix=f"{manager.scope.user_id}:{manager.scope.account_id}:{manager.scope.revision}:"
            digest=hashlib.sha256((prefix+encoded).encode()).hexdigest()
            result=await self.worker_api.post(f"/internal/executor/orders/{order['id']}/authorize",json={'attempt_id':work['attempt_id'],'prepared_digest':digest,'payload_json':encoded})
            self.assertEqual(422,result.status_code,result.text)
        self.assertEqual('pending',await self.pool.fetchval('SELECT state FROM v2_bet_orders WHERE id=$1',order['id']))

    async def test_exact_official_receipt_releases_only_matching_exposure(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        row=await self.reserve(signal)
        await self.pool.execute("UPDATE v2_bet_orders SET state='unknown',provider_order_no='EXACT',updated_at=now()-interval '1 minute' WHERE id=$1",row['id'])
        await self.api.post('/api/v1/accounts/leyu/refresh')
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        self.assertEqual('EXACT',work['orders'][0]['provider_order_no'])
        body={'attempt_id':work['attempt_id'],'status':'success','balance':'100','observed_at':datetime.now(timezone.utc).isoformat(),
              'receipts':[{'order_no':'WRONG','state':'settled','net_profit':'1.90','pnl_basis':'provider_reported_net'}]}
        url=f"/internal/executor/jobs/{work['job_id']}/finish"
        self.assertEqual(422,(await self.worker_api.post(url,json=body)).status_code)
        body['receipts'][0]['order_no']='EXACT'
        result=await self.worker_api.post(url,json=body)
        self.assertEqual(200,result.status_code,result.text)
        repeated=await self.worker_api.post(url,json=body)
        self.assertEqual(200,repeated.status_code,repeated.text)
        self.assertTrue(repeated.json()['idempotent'])
        stored=await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',row['id'])
        self.assertEqual(('settled',Decimal('0'),Decimal('1.90')),(stored['state'],stored['reserved_stake'],stored['settled_net']))

    async def test_same_provider_account_cannot_be_bound_by_another_user(self):
        await self.bind()
        other=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,$2,'其他','hash','user')",other,f'{other}@test.invalid')
        self.user['id']=other
        await self.recent_session()
        response=await self.api.put('/api/v1/accounts/leyu',json={'account_label':'另一绑定','username':'OWN-USER','password':'password'})
        self.assertEqual(409,response.status_code)


if __name__=='__main__':
    unittest.main()
