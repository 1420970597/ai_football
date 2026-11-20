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
from fastapi import FastAPI, HTTPException
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
            # The pool has already closed all owned connections. Ordinary DROP
            # also handles transient autovacuum workers without requesting the
            # extra cross-role termination privileges required by FORCE. A
            # leaked client connection must still make cleanup fail visibly.
            await admin.execute('DROP DATABASE '+self.dbname)
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

    async def test_failed_background_refresh_waits_before_dispatch_then_recovers(self):
        account=await self.healthy()
        await self.pool.execute("UPDATE v2_leyu_accounts SET last_verified_at=now()-interval '5 minutes' WHERE id=$1",account['id'])
        self.assertEqual(200,(await self.worker_api.post('/internal/executor/dispatch')).status_code)
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        self.assertEqual('account',work['kind'])
        prefix=f"/internal/executor/jobs/{work['job_id']}"
        body={'attempt_id':work['attempt_id'],'status':'failed','category':'account_http_rejected','provider_code':'403'}
        response=await self.worker_api.post(prefix+'/finish',json=body)
        self.assertEqual(200,response.status_code,response.text)
        failed=await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE id=$1',account['id'])
        self.assertGreater((failed['renewal_retry_after']-datetime.now(timezone.utc)).total_seconds(),50)
        for key in ('user_id','provider_identity_hash','binding_revision','config_revision','encrypted_session','health_state','renewal_state'):
            self.assertEqual(account[key],failed[key],key)
        self.assertEqual(200,(await self.worker_api.post('/internal/executor/dispatch')).status_code)
        self.assertEqual('idle',(await self.worker_api.post('/internal/executor/claim')).json()['kind'])
        repeated=await self.worker_api.post(prefix+'/finish',json=body)
        self.assertTrue(repeated.json()['idempotent'])
        self.assertEqual(failed['renewal_retry_after'],await self.pool.fetchval(
            'SELECT renewal_retry_after FROM v2_leyu_accounts WHERE id=$1',account['id']))
        self.assertEqual(409,(await self.worker_api.post(prefix+'/finish',json=body|{'category':'changed'})).status_code)
        await self.pool.execute("UPDATE v2_leyu_accounts SET renewal_retry_after=now()-interval '1 second' WHERE id=$1",account['id'])
        await self.worker_api.post('/internal/executor/dispatch')
        recovered=(await self.worker_api.post('/internal/executor/claim')).json()
        self.assertEqual('account',recovered['kind'])
        self.assertNotEqual(work['job_id'],recovered['job_id'])
        result=await self.worker_api.post(f"/internal/executor/jobs/{recovered['job_id']}/finish",json={
            'attempt_id':recovered['attempt_id'],'status':'success','balance':'98.50',
            'observed_at':datetime.now(timezone.utc).isoformat()})
        self.assertEqual(200,result.status_code,result.text)
        self.assertIsNone(await self.pool.fetchval('SELECT renewal_retry_after FROM v2_leyu_accounts WHERE id=$1',account['id']))
        await self.worker_api.post('/internal/executor/dispatch')
        self.assertEqual('idle',(await self.worker_api.post('/internal/executor/claim')).json()['kind'])

    async def test_distant_confirmed_order_waits_without_delaying_balance_or_urgent_checks(self):
        account = await self.healthy()
        fixture = 'leyu:886671'
        signal, expert, _, _ = await self.signal(fixture=fixture)
        await self.enable([expert])
        await self.pool.execute(
            "UPDATE v2_fixtures SET phase='scheduled',kickoff_at=now()+interval '1 hour' WHERE id=$1", fixture)
        await self.pool.execute("UPDATE v2_expert_signals SET phase='scheduled' WHERE id=$1", signal)
        order = await self.reserve(signal)
        await self.pool.execute(
            "UPDATE v2_bet_orders SET state='accepted',provider_order_no='DISTANT',"
            "result_payload=$2,updated_at=now()-interval '1 minute' WHERE id=$1",
            order['id'], {'order_no': 'DISTANT', 'state': 'unsettled'})
        dispatch, claim = '/internal/executor/dispatch', '/internal/executor/claim'

        async def finish(work):
            response = await self.worker_api.post(f"/internal/executor/jobs/{work['job_id']}/finish", json={
                'attempt_id': work['attempt_id'], 'status': 'success',
                'balance': '100', 'observed_at': datetime.now(timezone.utc).isoformat()})
            self.assertEqual(200, response.status_code, response.text)

        await self.worker_api.post(dispatch)
        self.assertEqual('idle', (await self.worker_api.post(claim)).json()['kind'])
        await self.pool.execute("UPDATE v2_leyu_accounts SET last_verified_at=now()-interval '61 seconds' WHERE id=$1",
                                account['id'])
        await self.worker_api.post(dispatch)
        work = (await self.worker_api.post(claim)).json()
        self.assertEqual(('account', []), (work['kind'], work['orders']))
        await finish(work)

        await self.pool.execute("UPDATE v2_fixtures SET kickoff_at=now()+interval '14 minutes' WHERE id=$1", fixture)
        await self.worker_api.post(dispatch)
        work = (await self.worker_api.post(claim)).json()
        self.assertEqual(['DISTANT'], [item['provider_order_no'] for item in work['orders']])
        await finish(work)

        await self.pool.execute("UPDATE v2_fixtures SET kickoff_at=now()+interval '1 hour' WHERE id=$1", fixture)
        await self.pool.execute("UPDATE v2_bet_orders SET state='unknown' WHERE id=$1", order['id'])
        await self.worker_api.post(dispatch)
        work = (await self.worker_api.post(claim)).json()
        self.assertEqual(['DISTANT'], [item['provider_order_no'] for item in work['orders']])
        stored = await self.pool.fetchrow('SELECT state,reserved_stake FROM v2_bet_orders WHERE id=$1', order['id'])
        self.assertEqual(('unknown', order['reserved_stake']), (stored['state'], stored['reserved_stake']))

    async def test_failed_refresh_preserves_longer_cooldown_and_other_accounts(self):
        account=await self.healthy()
        other_user,other_account=uuid.uuid4(),uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,$2,'隔离用户','hash','user')",other_user,f'{other_user}@test.invalid')
        await self.pool.execute("INSERT INTO v2_leyu_accounts(id,user_id,encrypted_credentials,account_label,health_state) VALUES($1,$2,'isolated','其他本人账户','unverified')",other_account,other_user)
        before_other=await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE id=$1',other_account)
        await self.pool.execute("UPDATE v2_leyu_accounts SET last_verified_at=now()-interval '5 minutes' WHERE id=$1",account['id'])
        await self.worker_api.post('/internal/executor/dispatch')
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        longer=await self.pool.fetchval("UPDATE v2_leyu_accounts SET renewal_retry_after=now()+interval '10 minutes' WHERE id=$1 RETURNING renewal_retry_after",account['id'])
        result=await self.worker_api.post(f"/internal/executor/jobs/{work['job_id']}/finish",json={
            'attempt_id':work['attempt_id'],'status':'failed','category':'account_api_unavailable'})
        self.assertEqual(200,result.status_code,result.text)
        self.assertEqual(longer,await self.pool.fetchval('SELECT renewal_retry_after FROM v2_leyu_accounts WHERE id=$1',account['id']))
        self.assertEqual(before_other,await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE id=$1',other_account))

    async def test_failed_reconcile_also_waits_without_renewing_session(self):
        account=await self.healthy()
        async with self.pool.acquire() as conn:
            await account_jobs.enqueue(conn,account,'reconcile')
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        result=await self.worker_api.post(f"/internal/executor/jobs/{work['job_id']}/finish",json={
            'attempt_id':work['attempt_id'],'status':'failed','category':'account_api_unavailable'})
        self.assertEqual(200,result.status_code,result.text)
        failed=await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE id=$1',account['id'])
        self.assertGreater(failed['renewal_retry_after'],datetime.now(timezone.utc))
        self.assertEqual(account['encrypted_session'],failed['encrypted_session'])
        self.assertEqual(('healthy','ready'),(failed['health_state'],failed['renewal_state']))

    async def test_failed_verify_and_unknown_do_not_become_background_retry(self):
        for status in ('failed','unknown'):
            with self.subTest(status=status):
                await self.bind()
                work=(await self.worker_api.post('/internal/executor/claim')).json()
                result=await self.worker_api.post(f"/internal/executor/jobs/{work['job_id']}/finish",json={
                    'attempt_id':work['attempt_id'],'status':status,'category':'account_api_unavailable'})
                self.assertEqual(200,result.status_code,result.text)
                account=await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE user_id=$1',self.user['id'])
                self.assertIsNone(account['renewal_retry_after'])
                self.assertEqual('unverified' if status=='failed' else 'renewal_required',account['health_state'])
                await self.worker_api.post('/internal/executor/dispatch')
                self.assertEqual('idle',(await self.worker_api.post('/internal/executor/claim')).json()['kind'])

    async def signal(self, *, fixture=None):
        source,expert,message,signal,quote=[uuid.uuid4() for _ in range(5)]
        fixture=fixture or 'leyu:'+uuid.uuid4().hex
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,$2,'群')",source,str(source))
        await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,$2,'专家昵称')",expert,str(expert))
        await self.pool.execute('INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)',source,expert)
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,'1','大2.5',now())",message,source,expert)
        await self.pool.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase,source_updated_at,home_score,away_score) VALUES($1,$1,'football','联赛','甲','乙',now(),'live',now(),0,0)",fixture)
        await self.pool.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,line,selection,price,source_updated_at,period,market_kind,status,play_id,market_instance_id,selection_id) "
                                "VALUES($1,$2,'2','全场大小',2.5,'over',1.95,now(),'full_time','total','active','2','market','option')",quote,fixture)
        await self.pool.execute("INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,line,selection,recommendation_price,quote_time,confidence,period,market_kind,score_baseline) "
                                "VALUES($1,$2,$3,$4,'live','2','全场大小',2.5,'over',1.95,now(),0.9,'full_time','total',$5)",signal,message,expert,fixture,{'home_score':0,'away_score':0})
        return signal,expert,message,quote

    async def enable(self,experts,daily='4',exposure='4',tz='Asia/Shanghai',sport_scope=None):
        self.settings.live_orders_enabled=True
        await self.pool.execute("INSERT INTO v2_admin_settings(setting_key,encrypted_value) VALUES('auto_betting_enabled',$1) ON CONFLICT(setting_key) DO UPDATE SET encrypted_value=EXCLUDED.encrypted_value",encrypt_secret(b'k'*32,'true'))
        plan=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_membership_plans(id,code,name,price,duration_days,enabled,entitlements) VALUES($1,$2,'VIP',10,30,TRUE,$3)",plan,str(plan),{'automatic_betting':True})
        await self.pool.execute("INSERT INTO v2_memberships(id,user_id,plan_id,status,starts_at,ends_at,grant_source) VALUES($1,$2,$3,'active',now()-interval '1 minute',now()+interval '1 day','test')",uuid.uuid4(),self.user['id'],plan)
        response=await self.api.put('/api/v1/accounts/leyu/auto-betting',json={'enabled':True,'confirmation_phrase':'I_UNDERSTAND','allowed_experts':[str(value) for value in experts],
            **({'auto_sport_scope':sport_scope} if sport_scope is not None else {}),
            'stake_per_order':'2','max_per_order':'2','daily_limit':daily,'open_exposure_limit':exposure,'daily_timezone':tz})
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(tz,response.json()['daily_timezone'])

    async def reserve(self,signal):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                return await orders.reserve_signal(conn,self.user['id'],signal,self.settings)

    async def followup_signal(self, original, *, line='2.5', selection='over', period='full_time'):
        signal, expert, _, quote = await self.signal()
        fixture = await self.pool.fetchval('SELECT fixture_id FROM v2_expert_signals WHERE id=$1', original)
        await self.pool.execute(
            'UPDATE v2_expert_signals SET fixture_id=$2,line=$3,selection=$4,period=$5 WHERE id=$1',
            signal, fixture, Decimal(line), selection, period)
        if (line, selection, period) != ('2.5', 'over', 'full_time'):
            await self.pool.execute(
                'UPDATE v2_current_quotes SET fixture_id=$2,line=$3,selection=$4,period=$5 WHERE id=$1',
                quote, fixture, Decimal(line), selection, period)
        return signal, expert

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
        self.assertEqual('Asia/Shanghai',visible['betting']['daily_timezone'])

    async def test_rejected_account_uses_updated_login_template_for_user_triggered_no_funds_retry(self):
        account=await self.bind()
        stale=encrypt_secret(b'k'*32,json.dumps({'leyu_app_host':'https://old-app.invalid',
            'leyu_app_token':'account-private-old-token','leyu_request_id':'old-session'}))
        await self.pool.execute("UPDATE v2_leyu_accounts SET encrypted_session=$2,health_state='rejected',renewal_state='rejected' WHERE id=$1",account['id'],stale)
        await self.pool.execute("UPDATE v2_admin_settings SET updated_at=now()-interval '1 hour' WHERE setting_key=ANY($1::text[])",
                                ['leyu_app_host','leyu_app_signing_config'])
        await self.pool.execute("UPDATE v2_account_jobs SET state='rejected',last_error='app_login_rejected',updated_at=now()-interval '10 minutes' WHERE account_id=$1",account['id'])
        visible=(await self.api.get('/api/v1/accounts/leyu')).json()
        self.assertFalse(visible['account']['reverification_available'])
        self.assertEqual(409,(await self.api.post('/api/v1/accounts/leyu/verify')).status_code)

        for key,value in {'leyu_app_host':'https://new-app.invalid',
                          'leyu_app_signing_config':'/run/secrets/new-signing.json'}.items():
            encrypted=encrypt_secret(b'k'*32,value)
            await self.pool.execute("UPDATE v2_admin_settings SET encrypted_value=$2,updated_at=now() WHERE setting_key=$1",key,encrypted)
        visible=(await self.api.get('/api/v1/accounts/leyu')).json()
        self.assertTrue(visible['account']['reverification_available'])
        queued=await self.api.post('/api/v1/accounts/leyu/verify')
        self.assertEqual(202,queued.status_code,queued.text)
        recovered=await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE id=$1',account['id'])
        self.assertEqual(('unverified','ready',None),(recovered['health_state'],recovered['renewal_state'],recovered['encrypted_session']))
        policy=await self.pool.fetchrow('SELECT auto_enabled,emergency_stopped FROM v2_user_bet_settings WHERE user_id=$1',self.user['id'])
        self.assertEqual((False,True),(policy['auto_enabled'],policy['emergency_stopped']))
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        self.assertEqual('https://new-app.invalid',work['config']['leyu_app_host'])
        self.assertNotIn('leyu_app_token',work['config'])

    async def test_unknown_account_remains_blocked_even_after_protocol_config_update(self):
        account=await self.bind()
        await self.pool.execute("UPDATE v2_leyu_accounts SET health_state='renewal_required',renewal_state='unknown' WHERE id=$1",account['id'])
        await self.pool.execute("UPDATE v2_account_jobs SET state='unknown',last_error='network_timeout',updated_at=now()-interval '1 day' WHERE account_id=$1",account['id'])
        await self.pool.execute("UPDATE v2_admin_settings SET updated_at=now() WHERE setting_key=ANY($1::text[])",
                                ['leyu_app_host','leyu_app_signing_config'])
        self.assertEqual(409,(await self.api.post('/api/v1/accounts/leyu/verify')).status_code)

    async def test_proxy_failure_retains_code_after_final_receipt_without_exposing_private_details(self):
        await self.bind()
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        prefix=f"/internal/executor/jobs/{work['job_id']}"
        claim=await self.worker_api.post(prefix+'/renewal/claim',json={
            'attempt_id':work['attempt_id'],'expected_revision':1,'identity_hash':hashlib.sha256(b'2001:own-user').hexdigest()})
        self.assertEqual(200,claim.status_code,claim.text)
        renewal=await self.worker_api.post(prefix+'/renewal/finish',json={
            'attempt_id':work['attempt_id'],'renewal_attempt':claim.json()['attempt_id'],'status':'failed',
            'metadata':{'category':'proxy_login_proxy_http_rejected','code':'410','login_submitted':False}})
        self.assertEqual(200,renewal.status_code,renewal.text)
        # Compatible with an older executor that sends no provider_code in the final receipt.
        body={'attempt_id':work['attempt_id'],'status':'failed','category':'proxy_login_proxy_http_rejected'}
        final=await self.worker_api.post(prefix+'/finish',json=body)
        self.assertEqual(200,final.status_code,final.text)
        self.assertEqual(200,(await self.worker_api.post(prefix+'/finish',json=body)).status_code)
        visible=(await self.api.get('/api/v1/accounts/leyu')).json()
        task=visible['tasks'][0]
        self.assertEqual('410',task['provider_code'])
        self.assertTrue(task['current_binding'])
        self.assertIn('账户登录请求未发出',task['message'])
        self.assertIn('created_at',task)
        for private in ('details','renewal_attempt','finish_sha256','own-password','own-user'):
            self.assertNotIn(private,json.dumps(visible))
        self.assertEqual(409,(await self.api.post('/api/v1/accounts/leyu/verify')).status_code)

    async def test_login_cooldown_prevents_user_queue_and_background_refresh_until_expiry(self):
        account=await self.healthy()
        await self.pool.execute("UPDATE v2_leyu_accounts SET last_verified_at=now()-interval '5 minutes',renewal_retry_after=now()+interval '60 seconds' WHERE id=$1",account['id'])
        self.assertEqual(409,(await self.api.post('/api/v1/accounts/leyu/verify')).status_code)
        result=await self.worker_api.post('/internal/executor/dispatch')
        self.assertEqual(200,result.status_code,result.text)
        self.assertEqual(0,await self.pool.fetchval("SELECT count(*) FROM v2_account_jobs WHERE account_id=$1 AND state='queued'",account['id']))
        await self.pool.execute("UPDATE v2_leyu_accounts SET renewal_retry_after=now()-interval '1 second' WHERE id=$1",account['id'])
        await self.worker_api.post('/internal/executor/dispatch')
        self.assertEqual(1,await self.pool.fetchval("SELECT count(*) FROM v2_account_jobs WHERE account_id=$1 AND state='queued'",account['id']))

    async def test_rebinding_marks_previous_task_history_and_keeps_new_task_current(self):
        account=await self.bind()
        await self.pool.execute("UPDATE v2_account_jobs SET state='rejected',last_error='app_login_rejected',details=$2 WHERE account_id=$1",account['id'],{'provider_code':'6026','private':'must-not-be-public'})
        await self.bind()
        visible=(await self.api.get('/api/v1/accounts/leyu')).json()
        self.assertTrue(visible['tasks'][0]['current_binding'])
        self.assertEqual('queued',visible['tasks'][0]['state'])
        self.assertFalse(visible['tasks'][1]['current_binding'])
        self.assertEqual('6026',visible['tasks'][1]['provider_code'])
        self.assertEqual('provider_identity_verification',visible['tasks'][1]['recovery_action'])
        self.assertIn('新设备身份验证',visible['tasks'][1]['message'])
        self.assertNotIn('must-not-be-public',json.dumps(visible))

    async def test_same_provider_identity_keeps_device_when_rebinding_or_changing_password(self):
        before=await self.bind()
        previous=account_jobs.decode_document(b'k'*32,before['encrypted_credentials'])
        response=await self.api.put('/api/v1/accounts/leyu',json={
            'account_label':'新名称','username':'OWN-USER','password':'updated-password'})
        self.assertEqual(200,response.status_code,response.text)
        after=await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE id=$1',before['id'])
        current=account_jobs.decode_document(b'k'*32,after['encrypted_credentials'])
        self.assertEqual(previous['device_uuid'],current['device_uuid'])
        self.assertEqual('updated-password',current['password'])
        self.assertGreater(after['binding_revision'],before['binding_revision'])
        self.assertIsNone(after['encrypted_session'])
        other=await self.api.put('/api/v1/accounts/leyu',json={
            'account_label':'另一账户','username':'other-provider-user','password':'other-password'})
        self.assertEqual(200,other.status_code,other.text)
        changed=await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE id=$1',before['id'])
        self.assertNotEqual(previous['device_uuid'],account_jobs.decode_document(b'k'*32,changed['encrypted_credentials'])['device_uuid'])

    async def device_challenge(self):
        account = await self.bind()
        await self.pool.execute("UPDATE v2_leyu_accounts SET health_state='rejected',renewal_state='rejected' WHERE id=$1", account['id'])
        await self.pool.execute("UPDATE v2_account_jobs SET state='rejected',last_error='app_login_rejected',details=$2 WHERE account_id=$1", account['id'], {'provider_code': '6026'})
        return account

    async def test_device_authorization_is_encrypted_one_use_and_not_public(self):
        account = await self.device_challenge()
        response = await self.api.post('/api/v1/accounts/leyu/device-authorization', json={'security_code': 'CODE1234'})
        self.assertEqual(202, response.status_code, response.text)
        task = await self.pool.fetchrow("SELECT * FROM v2_account_jobs WHERE account_id=$1 AND state='queued'", account['id'])
        self.assertNotIn('CODE1234', json.dumps(task['details']))
        self.assertEqual('CODE1234', decrypt_secret(b'k'*32, task['details']['encrypted_authorization_code']))
        self.assertNotIn('encrypted_authorization', (await self.api.get('/api/v1/accounts/leyu')).text)
        self.assertEqual(409, (await self.api.post('/api/v1/accounts/leyu/device-authorization', json={'security_code':'NEXT1234'})).status_code)
        work = (await self.worker_api.post('/internal/executor/claim')).json()
        self.assertEqual('CODE1234', work['config']['leyu_app_authorization_code'])
        stored = await self.pool.fetchval('SELECT details FROM v2_account_jobs WHERE id=$1', task['id'])
        self.assertNotIn('encrypted_authorization_code', stored)
        self.assertTrue(stored['device_authorization'])
        self.assertEqual('idle', (await self.worker_api.post('/internal/executor/claim')).json()['kind'])
        current = await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE id=$1', account['id'])
        self.assertEqual(account['binding_revision'], current['binding_revision'])
        self.assertEqual(account['encrypted_credentials'], current['encrypted_credentials'])

    async def test_device_authorization_expiry_removes_code_and_allows_fresh_code(self):
        account = await self.device_challenge()
        self.assertEqual(202, (await self.api.post('/api/v1/accounts/leyu/device-authorization', json={'security_code':'CODE1234'})).status_code)
        await self.pool.execute("UPDATE v2_account_jobs SET details=details||$2::jsonb WHERE account_id=$1 AND state='queued'", account['id'], {'authorization_expires_at': (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()})
        self.assertEqual('idle', (await self.worker_api.post('/internal/executor/claim')).json()['kind'])
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_account_jobs WHERE account_id=$1 AND details ? 'encrypted_authorization_code'", account['id']))
        self.assertEqual(202, (await self.api.post('/api/v1/accounts/leyu/device-authorization', json={'security_code':'NEXT1234'})).status_code)

    async def test_device_authorization_requires_own_challenge_active_session_and_known_state(self):
        account = await self.device_challenge()
        original = self.user['id']
        self.user['id'] = uuid.uuid4()
        self.assertEqual(404, (await self.api.post('/api/v1/accounts/leyu/device-authorization', json={'security_code':'CODE1234'})).status_code)
        self.user['id'] = original
        await self.pool.execute("UPDATE v2_leyu_accounts SET renewal_state='unknown' WHERE id=$1", account['id'])
        self.assertEqual(409, (await self.api.post('/api/v1/accounts/leyu/device-authorization', json={'security_code':'CODE1234'})).status_code)
        await self.pool.execute("UPDATE v2_leyu_accounts SET renewal_state='rejected',renewal_retry_after=now()+interval '60 seconds' WHERE id=$1", account['id'])
        self.assertEqual(409, (await self.api.post('/api/v1/accounts/leyu/device-authorization', json={'security_code':'CODE1234'})).status_code)
        await self.pool.execute("UPDATE v2_leyu_accounts SET renewal_retry_after=NULL WHERE id=$1", account['id'])
        await self.pool.execute('DELETE FROM v2_sessions WHERE user_id=$1', original)
        response = await self.api.post('/api/v1/accounts/leyu/device-authorization', json={'security_code':'CODE1234'})
        self.assertEqual(401, response.status_code, response.text)

    async def test_ambiguous_real_login_transport_stays_unknown_and_is_not_requeued(self):
        await self.bind()
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        work['config']['leyu_login_proxy_url']='http://sample:password@proxy.invalid:1234'
        login_calls=[]
        def app_http(request):
            if request.url.host=='ipv4.icanhazip.com':
                return httpx.Response(200,text='9.9.9.9\n')
            if request.url.path.endswith('/preInfo'):
                return httpx.Response(200,json={'status_code':6000,'data':{'ip':'1.1.1.1'}})
            self.assertTrue(request.url.path.endswith('/login'))
            login_calls.append(request)
            raise httpx.ReadTimeout('private-provider-body-and-credentials',request=request)
        executor=self.executor()
        executor.session_factory=lambda scope,config,hooks:AccountSessionManager(
            scope,config,hooks,direct_transport=httpx.MockTransport(app_http),
            login_transport_factory=lambda proxy:httpx.MockTransport(app_http),verify_public_hosts=False)
        document={'routes':{name:{'key':'k'*16,'iv':'i'*16} for name in ('','/site/api','/game/api')}}
        with patch('services.collector.leyu_session.private_document',return_value=document):
            await executor.account_job(work)
        visible=(await self.api.get('/api/v1/accounts/leyu')).json()
        self.assertEqual('unknown',visible['tasks'][0]['state'])
        self.assertEqual('unknown',visible['account']['renewal_state'])
        self.assertIn('停止重试',visible['tasks'][0]['message'])
        self.assertNotIn('private-provider',json.dumps(visible))
        await self.worker_api.post('/internal/executor/dispatch')
        self.assertEqual('idle',(await self.worker_api.post('/internal/executor/claim')).json()['kind'])
        self.assertEqual(1,len(login_calls))

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
        # An older worker's generic failed receipt must not erase the already
        # persisted unknown login outcome; replay remains idempotent.
        old_receipt={'attempt_id':work['attempt_id'],'status':'failed','category':'proxy_login_read_timeout'}
        result=await self.worker_api.post(prefix+'/finish',json=old_receipt)
        self.assertEqual(200,result.status_code,result.text)
        self.assertEqual('unknown',result.json()['status'])
        replay=await self.worker_api.post(prefix+'/finish',json=old_receipt)
        self.assertEqual(200,replay.status_code,replay.text)
        self.assertTrue(replay.json()['idempotent'])

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
        for key,value in {
            'leyu_login_proxy_url':'http://sample:password@geo.iproyal.com:12321',
        }.items():
            await self.pool.execute('INSERT INTO v2_admin_settings(setting_key,encrypted_value) VALUES($1,$2)',key,encrypt_secret(b'k'*32,value))
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        calls=[]
        proxy_urls=[]

        def app_http(request):
            calls.append(request)
            if request.url.host=='ipv4.icanhazip.com':
                return httpx.Response(200,text='9.9.9.9\n')
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
        def login_transport(proxy_url):
            proxy_urls.append(proxy_url)
            return httpx.MockTransport(app_http)
        executor.session_factory=lambda scope,config,hooks:AccountSessionManager(
            scope,config,hooks,direct_transport=httpx.MockTransport(app_http),
            login_transport_factory=login_transport,verify_public_hosts=False)
        document={'routes':{name:{'key':'k'*16,'iv':'i'*16} for name in ('','/site/api','/game/api')}}
        with patch('services.collector.leyu_session.private_document',return_value=document):
            await executor.account_job(work)
        account=await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE user_id=$1',self.user['id'])
        self.assertEqual(('healthy',3,'ready'),(account['health_state'],account['config_revision'],account['renewal_state']))
        private=json.loads(decrypt_secret(b'k'*32,account['encrypted_session']))
        self.assertEqual('private-own-app-token',private['leyu_app_token'])
        self.assertEqual('own-business-session',private['leyu_request_id'])
        self.assertEqual(1,sum(request.url.path.endswith('/login') for request in calls))
        self.assertEqual('geo.iproyal.com',urlsplit(proxy_urls[0]).hostname)
        self.assertTrue('_session-' in urlsplit(proxy_urls[0]).password)
        self.assertNotIn(BET_PATH,[request.url.path for request in self.native_requests])

    async def test_http_expired_refresh_relaunches_own_app_token_and_preserves_consent(self):
        account=await self.healthy()
        _,expert,_,_=await self.signal()
        await self.enable([expert])
        policy=await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1',self.user['id'])
        session={'leyu_api_base':'https://venue.invalid','leyu_origin':'https://ui.invalid',
                 'leyu_request_id':'expired-own-session','leyu_app_token':'own-existing-app-token'}
        await self.pool.execute("UPDATE v2_leyu_accounts SET encrypted_session=$2,last_verified_at=now()-interval '5 minutes' WHERE id=$1",
                                account['id'],encrypt_secret(b'k'*32,json.dumps(session)))
        await self.worker_api.post('/internal/executor/dispatch')
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        self.assertEqual('account',work['kind'])
        calls=[]
        def app_http(request):
            calls.append(request)
            if request.url.path.endswith('/preInfo'):
                return httpx.Response(200,json={'status_code':6000,'data':{'ip':'1.1.1.1'}})
            self.assertTrue(request.url.path.endswith('/launch'),request.url.path)
            self.assertEqual('own-existing-app-token',request.headers['x-api-token'])
            self.assertEqual('false',json.loads(request.content)['sendMoney'])
            return httpx.Response(200,json={'status_code':6000,'data':{
                'url':'https://venue.invalid/index?token=renewed-own-session','h5Url':'https://ui.invalid'}})
        def venue_http(request):
            calls.append(request)
            self.assertEqual(AMOUNT_PATH,request.url.path)
            if request.headers['requestId']=='expired-own-session':
                return httpx.Response(401,json={'code':'0401013','msg':'private-expiry-response'})
            self.assertEqual('renewed-own-session',request.headers['requestId'])
            return httpx.Response(200,json={'amount':'100'})
        executor=Executor(self.worker_api,'https://platform.invalid/internal/executor',
            client_factory=lambda scope,session,**kw:AccountProtocolClient(scope,session,transport=httpx.MockTransport(venue_http),**kw),
            session_factory=lambda scope,config,hooks:AccountSessionManager(scope,config,hooks,
                direct_transport=httpx.MockTransport(app_http),verify_public_hosts=False))
        document={'routes':{name:{'key':'k'*16,'iv':'i'*16} for name in ('','/site/api','/game/api')}}
        with patch('services.collector.leyu_session.private_document',return_value=document):
            await executor.account_job(work)
        current=await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE id=$1',account['id'])
        self.assertEqual(('healthy','ready',2),(current['health_state'],current['renewal_state'],current['config_revision']))
        self.assertGreater(current['last_verified_at'],account['last_verified_at'])
        self.assertEqual('renewed-own-session',account_jobs.decode_document(b'k'*32,current['encrypted_session'])['leyu_request_id'])
        self.assertEqual(policy,await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1',self.user['id']))
        self.assertEqual('succeeded',await self.pool.fetchval('SELECT state FROM v2_account_jobs WHERE id=$1',uuid.UUID(work['job_id'])))
        self.assertEqual(2,sum(request.url.path==AMOUNT_PATH for request in calls))
        self.assertEqual(1,sum(request.url.path.endswith('/launch') for request in calls))
        self.assertFalse(any(request.url.path.endswith('/login') or request.url.path==BET_PATH for request in calls))
        async with self.pool.acquire() as conn,conn.transaction():
            await orders.account_policy(conn,self.user['id'],self.settings)

    async def test_http_permission_denied_refresh_never_renews_or_sends(self):
        account=await self.healthy()
        await self.api.post('/api/v1/accounts/leyu/refresh')
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        manager=AccountSessionManager(AccountScope(**work['scope']),work['config'],RenewalHooks(None,None,None))
        manager.renew=AsyncMock()
        executor=Executor(self.worker_api,'https://platform.invalid/internal/executor',
            session_factory=lambda *args:manager,client_factory=lambda scope,session,**kw:AccountProtocolClient(
                scope,session,transport=httpx.MockTransport(lambda request:httpx.Response(403,json={'code':'0401015'})),**kw))
        await executor.account_job(work)
        manager.renew.assert_not_awaited()
        job=await self.pool.fetchrow('SELECT * FROM v2_account_jobs WHERE id=$1',uuid.UUID(work['job_id']))
        self.assertEqual(('failed','account_http_rejected','403'),(job['state'],job['last_error'],job['details']['provider_code']))
        self.assertEqual(account['encrypted_session'],await self.pool.fetchval('SELECT encrypted_session FROM v2_leyu_accounts WHERE id=$1',account['id']))

    async def test_reservation_concurrent_idempotent_limits(self):
        await self.healthy()
        s1,e1,_,_=await self.signal()
        s2,e2,_,_=await self.signal()
        await self.enable([e1,e2],daily='2',exposure='2',tz='UTC')
        duplicates=await asyncio.gather(*(self.reserve(s1) for _ in range(3)))
        self.assertEqual(1,len({row['id'] for row in duplicates}))
        with self.assertRaises(Exception):
            await self.reserve(s2)
        self.assertEqual(Decimal('2'),await self.pool.fetchval('SELECT sum(reserved_stake) FROM v2_bet_orders'))

    async def test_dispatch_logs_aggregated_reservation_rejections(self):
        await self.healthy()
        first, expert1, _, _ = await self.signal()
        _, expert2, _, missing_quote = await self.signal()
        _, expert3, _, _ = await self.signal()
        await self.enable([expert1, expert2, expert3], daily='2', exposure='2', tz='UTC')
        await self.pool.execute('DELETE FROM v2_current_quotes WHERE id=$1', missing_quote)
        endpoint = '/internal/executor/dispatch'

        with self.assertLogs(accounts.log, level='INFO') as logged:
            response = await self.worker_api.post(endpoint)
        self.assertEqual((200, {'reserved': 1}), (response.status_code, response.json()))
        self.assertEqual(['auto_reservation_rejections candidates=3 reasons={"daily_limit": 1, "market_or_signal_unavailable": 1}'],
                         [record.getMessage() for record in logged.records])
        self.assertEqual(first, await self.pool.fetchval('SELECT signal_id FROM v2_bet_orders'))

        await self.pool.execute('UPDATE v2_user_bet_settings SET daily_limit=4 WHERE user_id=$1', self.user['id'])
        with self.assertLogs(accounts.log, level='INFO') as logged:
            response = await self.worker_api.post(endpoint)
        self.assertEqual((200, {'reserved': 0}), (response.status_code, response.json()))
        self.assertEqual(['auto_reservation_rejections candidates=2 reasons={"market_or_signal_unavailable": 1, "open_exposure_limit": 1}'],
                         [record.getMessage() for record in logged.records])
        self.assertEqual(Decimal('2'), await self.pool.fetchval('SELECT sum(reserved_stake) FROM v2_bet_orders'))

    async def test_distinct_experts_cannot_reserve_identical_or_nearby_open_orders(self):
        await self.healthy()
        first, expert, _, _ = await self.signal(fixture='leyu:887765')
        same, same_expert = await self.followup_signal(first)
        close, close_expert = await self.followup_signal(first, line='2.75')
        far, far_expert = await self.followup_signal(first, line='3.5')
        opposite, opposite_expert = await self.followup_signal(first, selection='under')
        another_period, period_expert = await self.followup_signal(first, period='first_half')
        await self.enable([expert, same_expert, close_expert, far_expert, opposite_expert, period_expert],
                          daily='20', exposure='20')
        results = await asyncio.gather(self.reserve(first), self.reserve(same), return_exceptions=True)
        rejected = [value for value in results if isinstance(value, orders.OrderEligibilityError)]
        self.assertEqual(['duplicate_economic_order'], [value.reason for value in rejected])
        row, = [value for value in results if not isinstance(value, Exception)]
        with self.assertRaises(orders.OrderEligibilityError) as duplicate:
            await self.reserve(close)
        self.assertEqual('nearby_open_order', duplicate.exception.reason)
        await self.reserve(far)
        await self.reserve(opposite)
        await self.reserve(another_period)
        await self.pool.execute("UPDATE v2_bet_orders SET state='unknown' WHERE id=$1", row['id'])
        with self.assertRaises(orders.OrderEligibilityError) as unknown:
            await self.reserve(close)
        self.assertEqual('nearby_open_order', unknown.exception.reason)
        self.assertEqual(4, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_known_period_total_duplicates_cross_phase_and_legacy_queue_is_rechecked(self):
        await self.healthy()
        scheduled, expert, _, _ = await self.signal(fixture='leyu:887766')
        live, live_expert = await self.followup_signal(scheduled)
        await self.enable([expert, live_expert], daily='12', exposure='12')
        await self.pool.execute("UPDATE v2_fixtures SET phase='scheduled' WHERE id='leyu:887766'")
        await self.pool.execute("UPDATE v2_expert_signals SET phase='scheduled' WHERE id=$1", scheduled)
        first = await self.reserve(scheduled)
        await self.pool.execute(
            "UPDATE v2_bet_orders SET state='accepted',provider_order_no='original' WHERE id=$1", first['id'])
        await self.pool.execute("UPDATE v2_fixtures SET phase='live' WHERE id='leyu:887766'")
        with self.assertRaises(orders.OrderEligibilityError) as duplicate:
            await self.reserve(live)
        self.assertEqual('duplicate_economic_order', duplicate.exception.reason)
        await self.pool.execute(
            "UPDATE v2_bet_orders SET state='rejected',reserved_stake=0 WHERE id=$1",first['id'])
        later = await self.reserve(live)
        await self.pool.execute("UPDATE v2_bet_orders SET state='unknown',reserved_stake=stake WHERE id=$1",first['id'])
        claim = await self.worker_api.post('/internal/executor/claim?live_capable=true')
        self.assertEqual({'kind':'idle'}, claim.json())
        stored = await self.pool.fetchrow('SELECT state,reserved_stake,result_payload FROM v2_bet_orders WHERE id=$1',later['id'])
        self.assertEqual(('cancelled', Decimal('0'), 'duplicate_economic_order'),
                         (stored['state'], stored['reserved_stake'], stored['result_payload']['reason']))

    async def test_live_handicap_matches_final_margin_across_phase_and_score_changes(self):
        await self.healthy()
        fixture = 'leyu:887767'
        scheduled, expert, _, first_quote = await self.signal(fixture=fixture)
        same_at_zero, expert_zero = await self.followup_signal(scheduled)
        same_after_goal, expert_goal = await self.followup_signal(scheduled, line='0.25', selection='home')
        nearby_after_goal, expert_nearby = await self.followup_signal(scheduled, line='0.75', selection='home')
        different_after_goal, expert_other = await self.followup_signal(scheduled, line='-1.25', selection='home')
        await self.enable([expert, expert_zero, expert_goal, expert_nearby, expert_other], daily='12', exposure='12')
        await self.pool.execute("UPDATE v2_fixtures SET phase='scheduled' WHERE id=$1", fixture)
        await self.pool.execute(
            "UPDATE v2_expert_signals SET phase='scheduled',selection='home',line=-0.75 WHERE id=$1", scheduled)
        await self.pool.execute(
            "UPDATE v2_expert_signals SET selection='home',line=-0.75,score_baseline=$2 WHERE id=$1",
            same_at_zero, {'home': 0, 'away': 0})
        await self.pool.execute(
            "UPDATE v2_expert_signals SET score_baseline=$2 WHERE id=ANY($1::uuid[])",
            [same_after_goal, nearby_after_goal, different_after_goal], {'home': 1, 'away': 0})
        await self.pool.execute(
            "UPDATE v2_expert_signals SET market_id='4',market_kind='handicap' WHERE fixture_id=$1", fixture)
        await self.pool.execute(
            "UPDATE v2_current_quotes SET line=-0.75,selection='home' WHERE id=$1", first_quote)
        await self.pool.execute(
            "UPDATE v2_current_quotes SET market_id='4',market_kind='handicap',play_id='4' WHERE fixture_id=$1", fixture)
        first = await self.reserve(scheduled)
        await self.pool.execute(
            "UPDATE v2_bet_orders SET state='accepted',provider_order_no='original-handicap' WHERE id=$1",
            first['id'])
        await self.pool.execute("UPDATE v2_fixtures SET phase='live' WHERE id=$1", fixture)
        with self.assertRaises(orders.OrderEligibilityError) as duplicate:
            await self.reserve(same_at_zero)
        self.assertEqual('duplicate_economic_order', duplicate.exception.reason)
        await self.pool.execute("UPDATE v2_fixtures SET home_score=1 WHERE id=$1", fixture)
        with self.assertRaises(orders.OrderEligibilityError) as shifted:
            await self.reserve(same_after_goal)
        self.assertEqual('duplicate_economic_order', shifted.exception.reason)
        with self.assertRaises(orders.OrderEligibilityError) as nearby:
            await self.reserve(nearby_after_goal)
        self.assertEqual('nearby_open_order', nearby.exception.reason)
        distinct = await self.reserve(different_after_goal)
        self.assertEqual(different_after_goal, distinct['signal_id'])
        self.assertEqual(2, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))
        await self.pool.execute("UPDATE v2_bet_orders SET state='settled',reserved_stake=0 WHERE id=$1", first['id'])
        later = await self.reserve(nearby_after_goal)
        await self.pool.execute("UPDATE v2_bet_orders SET state='unknown',reserved_stake=stake WHERE id=$1", first['id'])
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                with self.assertRaises(orders.OrderEligibilityError) as recheck:
                    await orders.recheck_order(connection, later, self.settings)
        self.assertEqual('nearby_open_order', recheck.exception.reason)

    async def test_many_distinct_recommendations_never_exceed_limits_and_stop_is_available(self):
        await self.healthy()
        signals, experts = [], []
        for _ in range(6):
            signal, expert, _, _ = await self.signal()
            signals.append(signal)
            experts.append(expert)
        await self.enable(experts, daily='4', exposure='4', tz='UTC')
        results = await asyncio.gather(*(self.reserve(signal) for signal in signals), return_exceptions=True)
        successful = [result for result in results if not isinstance(result, Exception)]
        rejected = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(2, len(successful))
        self.assertEqual(4, len(rejected))
        self.assertEqual(2, await self.pool.fetchval("SELECT count(*) FROM v2_bet_orders WHERE state='pending'"))
        self.assertEqual(Decimal('4'), await self.pool.fetchval('SELECT sum(reserved_stake) FROM v2_bet_orders'))
        stopped = await self.api.post('/api/v1/accounts/leyu/emergency-stop')
        self.assertEqual(200, stopped.status_code, stopped.text)
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_bet_orders WHERE state='pending'"))
        self.assertEqual(Decimal('0'), await self.pool.fetchval('SELECT coalesce(sum(reserved_stake),0) FROM v2_bet_orders'))

    async def test_daily_calendar_boundaries_respect_dst_and_reject_naive_time(self):
        async with self.pool.acquire() as conn:
            for instant,expected_hours in [('2026-03-29T12:00:00+00:00',23),('2026-10-25T12:00:00+00:00',25)]:
                row=await orders.exposure_totals(conn,self.user['id'],'Europe/Berlin',datetime.fromisoformat(instant))
                self.assertEqual(expected_hours,(row['day_ends_at']-row['day_starts_at']).total_seconds()/3600)
                self.assertEqual((Decimal('0'),Decimal('0')),(row['daily'],row['exposure']))
            with self.assertRaises(ValueError):
                await orders.exposure_totals(conn,self.user['id'],'UTC',datetime(2026,10,26))

    async def test_daily_timezone_counts_unknown_and_retains_older_open_exposure(self):
        for created,state,reserved in [('2026-10-25T17:00:00+00:00','unknown','2'),
                                       ('2026-10-26T00:00:00+00:00','accepted','2'),
                                       ('2026-10-26T00:15:00+00:00','cancelled','0'),
                                       ('2026-09-01T00:00:00+00:00','unknown','2')]:
            signal,_,_,_=await self.signal()
            await self.pool.execute('INSERT INTO v2_bet_orders(id,user_id,signal_id,idempotency_key,state,stake,price,reserved_stake,created_at) '
                                    'VALUES($1,$2,$3,$4,$5,2,1.95,$6,$7)',uuid.uuid4(),self.user['id'],signal,str(signal),state,Decimal(reserved),datetime.fromisoformat(created))
        moment=datetime.fromisoformat('2026-10-26T00:30:00+00:00')
        async with self.pool.acquire() as conn:
            shanghai=await orders.exposure_totals(conn,self.user['id'],'Asia/Shanghai',moment)
            utc=await orders.exposure_totals(conn,self.user['id'],'UTC',moment)
        self.assertEqual(Decimal('4'),shanghai['daily'])
        self.assertEqual(Decimal('2'),utc['daily'])
        self.assertEqual(Decimal('6'),shanghai['exposure'])
        self.assertEqual(shanghai['exposure'],utc['exposure'])
        self.assertEqual(datetime.fromisoformat('2026-10-25T16:00:00+00:00'),shanghai['day_starts_at'])

    async def test_timezone_update_reauthorizes_and_invalid_value_preserves_current_policy(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        order=await self.reserve(signal)
        previous=await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1',self.user['id'])
        body={'enabled':True,'confirmation_phrase':'I_UNDERSTAND','allowed_experts':[str(expert)],
              'stake_per_order':'2','max_per_order':'2','daily_limit':'4','open_exposure_limit':'4','daily_timezone':'Not/AZone'}
        result=await self.api.put('/api/v1/accounts/leyu/auto-betting',json=body)
        self.assertEqual(422,result.status_code,result.text)
        unchanged=await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1',self.user['id'])
        self.assertEqual(previous,unchanged)
        self.assertEqual('pending',await self.pool.fetchval('SELECT state FROM v2_bet_orders WHERE id=$1',order['id']))
        body['daily_timezone']='Europe/Berlin'
        result=await self.api.put('/api/v1/accounts/leyu/auto-betting',json=body)
        self.assertEqual(200,result.status_code,result.text)
        current=await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1',self.user['id'])
        self.assertEqual(('Europe/Berlin',previous['revision']+1),(current['daily_timezone'],current['revision']))
        self.assertEqual('cancelled',await self.pool.fetchval('SELECT state FROM v2_bet_orders WHERE id=$1',order['id']))
        self.assertEqual('Europe/Berlin',(await self.api.get('/api/v1/accounts/leyu')).json()['betting']['daily_timezone'])
        audit=await self.pool.fetchval("SELECT details FROM v2_audit_log WHERE action='auto_betting_authorized' ORDER BY created_at DESC LIMIT 1")
        self.assertEqual('Europe/Berlin',audit['daily_timezone'])

    async def test_signal_source_revision_stale_and_membership_rechecked(self):
        await self.healthy()
        signal,expert,message,quote=await self.signal()
        await self.enable([expert])
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '2 minutes' WHERE id=$1",quote)
        order = await self.reserve(signal)
        self.assertEqual('pending', order['state'])
        await self.pool.execute('DELETE FROM v2_bet_orders WHERE id=$1', order['id'])
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

    async def test_stale_expert_price_reserves_market_and_uses_current_provider_price(self):
        await self.healthy()
        signal,expert,_,quote=await self.signal()
        await self.enable([expert])
        fixture_received_at = await self.pool.fetchval(
            "UPDATE v2_fixtures SET source_updated_at=NULL,source_time_quality='unknown' "
            "WHERE id=(SELECT fixture_id FROM v2_expert_signals WHERE id=$1) RETURNING received_at", signal)
        await self.pool.execute("UPDATE v2_current_quotes SET price=1.50,status='closed',source_updated_at=now()-interval '10 minutes' WHERE id=$1",quote)
        reserved=(await self.worker_api.post('/internal/executor/dispatch')).json()
        self.assertEqual(1,reserved['reserved'])
        work=(await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        self.assertEqual('order',work['kind'])
        calls=[]
        def live_provider(request):
            calls.append(request)
            if request.url.path==LATEST_MARKET_PATH:
                query=json.loads(request.content)['idList'][0]
                self.assertEqual('option',query['oddsId'])
                return httpx.Response(200,json={'code':0,'data':[{'id':'market','matchInfoId':query['matchInfoId'],'playId':'2',
                    'matchStatus':1,'matchOver':0,'matchHandicapStatus':0,'status':0,'marketValue':'2.5',
                    'marketOddsList':[{'id':'option-current','oddsType':'Over','oddsStatus':1,'oddsValue':'235000'}]}]})
            if request.url.path==LIMITS_PATH:
                detail=json.loads(request.content)['orderMaxBetMoney'][0]
                self.assertEqual(('option-current','235000'),(detail['playOptionId'],detail['oddsValue']))
                return httpx.Response(200,json={'code':0,'data':[{'code':0,'playOptionsId':'option-current','minBet':'1','orderMaxPay':'10'}]})
            if request.url.path==AMOUNT_PATH:
                return httpx.Response(200,json={'amount':'100'})
            if request.url.path==BET_PATH:
                body=json.loads(request.content)['seriesOrders'][0]['orderDetailList'][0]
                self.assertEqual(('option-current','2.35','235000'),(body['playOptionsId'],body['oddFinally'],body['odds']))
                self.assertEqual('0:0', body['scoreBenchmark'])
                return httpx.Response(200,json={'code':0,'data':{'orderDetailRespList':[{'orderNo':'LIVE-PRICE','orderStatusCode':1}]}})
            raise AssertionError(request.url.path)
        executor=Executor(self.worker_api,'https://platform.invalid/internal/executor',live_orders_enabled=True,
            client_factory=lambda scope,session,**kwargs:AccountProtocolClient(scope,session,transport=httpx.MockTransport(live_provider),**kwargs))
        await executor.order_job(work)
        order=await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',uuid.UUID(work['order_id']))
        self.assertEqual(('accepted',Decimal('2.35'),'LIVE-PRICE'),(order['state'],order['price'],order['provider_order_no']))
        self.assertEqual(('option-current',Decimal('2.35'),Decimal('1.95')),
            (order['quote_snapshot']['selection_id'],Decimal(order['quote_snapshot']['price']),Decimal(order['quote_snapshot']['expert_recommendation_price'])))
        self.assertTrue(order['quote_snapshot']['provider_quote_checked_at'])
        evidence=await self.pool.fetch("SELECT evidence FROM v2_quote_evidence WHERE evidence_kind='order_attempt' AND reference_id=$1 ORDER BY evidence->>'stage'",order['id'])
        self.assertEqual(['2.35','2.35'],[item['evidence']['price'] for item in evidence])
        self.assertTrue(all(item['evidence']['odds_change_policy']=='accept_current_provider_quote' for item in evidence))
        for item in evidence:
            self.assertEqual({'home': 0, 'away': 0}, item['evidence']['score_baseline'])
            self.assertEqual({'received_at': fixture_received_at.astimezone(timezone.utc).isoformat(),
                              'source_updated_at': None, 'source_time_quality': 'unknown'},
                             item['evidence']['fixture_projection'])
        self.assertEqual(1,sum(request.url.path==BET_PATH for request in calls))

    async def test_live_total_requires_known_matching_scores_through_authorization(self):
        await self.healthy()
        fixture = 'leyu:234567'
        signal, expert, _, _ = await self.signal(fixture=fixture)
        await self.enable([expert])
        await self.pool.execute('UPDATE v2_expert_signals SET score_baseline=$2 WHERE id=$1', signal, {'home': 1, 'away': 0})
        await self.pool.execute('UPDATE v2_fixtures SET home_score=NULL WHERE id=$1', fixture)
        with self.assertRaisesRegex(HTTPException, '推荐比分缺失或已经变化'):
            await self.reserve(signal)
        await self.pool.execute('UPDATE v2_fixtures SET home_score=2 WHERE id=$1', fixture)
        with self.assertRaisesRegex(HTTPException, '推荐比分缺失或已经变化'):
            await self.reserve(signal)
        await self.pool.execute('UPDATE v2_fixtures SET home_score=1 WHERE id=$1', fixture)
        order = await self.reserve(signal)
        self.assertEqual('1:0', order['quote_snapshot']['score_benchmark'])
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                legacy_snapshot = dict(order['quote_snapshot'])
                del legacy_snapshot['score_benchmark']
                legacy_order = dict(order, quote_snapshot=legacy_snapshot)
                with self.assertRaisesRegex(HTTPException, '预留比分缺失或已经变化'):
                    await orders.recheck_order(connection, legacy_order, self.settings)
        work = (await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        self.assertEqual('order', work['kind'])
        for changed in (None, 2):
            await self.pool.execute('UPDATE v2_fixtures SET home_score=$2 WHERE id=$1', fixture, changed)
            async with self.pool.acquire() as connection:
                async with connection.transaction():
                    with self.assertRaisesRegex(HTTPException, '推荐比分缺失或已经变化'):
                        await orders.recheck_order(connection, order, self.settings)
        await self.executor().order_job(work)
        self.assertNotIn(BET_PATH, [request.url.path for request in self.native_requests])
        self.assertEqual('cancelled', await self.pool.fetchval('SELECT state FROM v2_bet_orders WHERE id=$1', order['id']))

    async def test_live_under_checks_win_potential_at_reservation_and_recheck(self):
        await self.healthy()
        fixture = 'leyu:234568'
        signal, expert, _, quote = await self.signal(fixture=fixture)
        await self.enable([expert])
        await self.pool.execute("UPDATE v2_expert_signals SET selection='under',score_baseline=$2 WHERE id=$1",
                                signal, {'home': 2, 'away': 0})
        await self.pool.execute("UPDATE v2_current_quotes SET selection='under' WHERE id=$1", quote)
        await self.pool.execute('UPDATE v2_fixtures SET home_score=2 WHERE id=$1', fixture)
        order = await self.reserve(signal)
        await self.pool.execute('UPDATE v2_fixtures SET home_score=3 WHERE id=$1', fixture)
        await self.pool.execute('UPDATE v2_expert_signals SET score_baseline=$2 WHERE id=$1',
                                signal, {'home': 3, 'away': 0})
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                with self.assertRaises(orders.OrderEligibilityError) as error:
                    await orders.recheck_order(connection, order, self.settings)
                self.assertEqual('live_under_cannot_win', error.exception.reason)
        work = (await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        self.assertNotEqual('order', work['kind'])
        self.assertEqual('cancelled', await self.pool.fetchval('SELECT state FROM v2_bet_orders WHERE id=$1', order['id']))
        self.assertNotIn(BET_PATH, [request.url.path for request in self.native_requests])

        later, _ = await self.followup_signal(signal, line='1.75', selection='under', period='first_half')
        await self.pool.execute("UPDATE v2_expert_signals SET market_id='18',score_baseline=$2 WHERE id=$1",
                                later, {'home': 3, 'away': 0})
        await self.pool.execute("UPDATE v2_current_quotes SET market_id='18',play_id='18' "
                                "WHERE fixture_id=$1 AND period='first_half'", fixture)
        await self.pool.execute('UPDATE v2_fixtures SET half_home_score=2,half_away_score=0 WHERE id=$1', fixture)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                eligible = dict(await orders.eligible_signal(connection, later))
                self.assertTrue(orders.cannot_win_live_under(eligible))
                self.assertTrue(orders.cannot_win_live_under(dict(eligible, line=Decimal('2'))))
                self.assertFalse(orders.cannot_win_live_under(dict(eligible, line=Decimal('2.25'))))
                self.assertFalse(orders.cannot_win_live_under(dict(eligible, half_home_score=None)))
                self.assertFalse(orders.cannot_win_live_under(dict(eligible, current_basis='remaining_period')))
                self.assertIsNone(orders.checked_score_benchmark(eligible, manual=True))

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
            prepared=await client.preflight(NativeSelection.from_snapshot(work['selection']),Decimal(work['stake']),device_imei=work['config']['leyu_app_uuid'])
        for field,value in [('marketValue','3.5'),('betAmount','3.00'),('scoreBenchmark','9:9'),('playOptions','Under')]:
            payload=prepared.payload
            payload['seriesOrders'][0]['orderDetailList'][0][field]=value
            encoded=json.dumps(payload,sort_keys=True,separators=(',',':'))
            prefix=f"{manager.scope.user_id}:{manager.scope.account_id}:{manager.scope.revision}:"
            digest=hashlib.sha256((prefix+prepared.checked_at+":"+encoded).encode()).hexdigest()
            result=await self.worker_api.post(f"/internal/executor/orders/{order['id']}/authorize",json={'attempt_id':work['attempt_id'],'prepared_digest':digest,'payload_json':encoded,'quote_checked_at':prepared.checked_at})
            self.assertEqual(422,result.status_code,result.text)
        self.assertEqual('pending',await self.pool.fetchval('SELECT state FROM v2_bet_orders WHERE id=$1',order['id']))

    async def test_remaining_period_requires_consistent_scores_at_reserve_and_send(self):
        from services.executor.account_protocol import NativeSelection

        await self.healthy()
        fixture = 'leyu:123456'
        signal, expert, _, quote = await self.signal(fixture=fixture)
        await self.enable([expert])
        await self.pool.execute(
            "UPDATE v2_current_quotes SET market_id='4',market_kind='handicap',selection='home',line=-0.5,play_id='4' WHERE id=$1", quote)
        await self.pool.execute(
            "UPDATE v2_expert_signals SET market_id='4',market_kind='handicap',selection='home',line=-0.5,score_baseline=$2 WHERE id=$1",
            signal, {'home': 1, 'away': 0})
        with self.assertRaisesRegex(HTTPException, '推荐比分缺失或已经变化'):
            await self.reserve(signal)
        await self.pool.execute('UPDATE v2_fixtures SET home_score=1,away_score=0 WHERE id=$1', fixture)
        order = await self.reserve(signal)
        self.assertEqual('1:0', order['quote_snapshot']['score_benchmark'])
        work = (await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        self.assertEqual('order', work['kind'])

        def native_handicap(request):
            response = self.native(request)
            if request.url.path != LATEST_MARKET_PATH:
                return response
            payload = response.json()
            market = payload['data'][0]
            market.update(playId='4', marketValue='-0.5')
            market['marketOddsList'][0]['oddsType'] = '1'
            return httpx.Response(200, json=payload)

        manager = AccountSessionManager(AccountScope(**work['scope']), work['config'], RenewalHooks(None,None,None))
        async with AccountProtocolClient(manager.scope, manager.venue_session(), transport=httpx.MockTransport(native_handicap)) as client:
            prepared = await client.preflight(NativeSelection.from_snapshot(work['selection']), Decimal(work['stake']),
                                              device_imei=work['config']['leyu_app_uuid'])
        body = {'attempt_id': work['attempt_id'], 'prepared_digest': prepared.digest,
                'payload_json': prepared.payload_json, 'quote_checked_at': prepared.checked_at}
        url = f"/internal/executor/orders/{order['id']}/authorize"
        await self.pool.execute('UPDATE v2_fixtures SET home_score=2 WHERE id=$1', fixture)
        rejected = await self.worker_api.post(url, json=body)
        self.assertEqual(409, rejected.status_code, rejected.text)
        self.assertIn('推荐比分缺失或已经变化', rejected.json()['detail'])
        self.assertEqual('pending', await self.pool.fetchval('SELECT state FROM v2_bet_orders WHERE id=$1', order['id']))
        projection = await self.pool.fetchrow(
            "UPDATE v2_fixtures SET home_score=1,source_updated_at=now()-interval '1 second',"
            "received_at=now(),source_time_quality='known' WHERE id=$1 RETURNING source_updated_at,received_at", fixture)
        authorized = await self.worker_api.post(url, json=body)
        self.assertEqual(200, authorized.status_code, authorized.text)
        stages = await self.pool.fetch(
            "SELECT evidence FROM v2_quote_evidence WHERE reference_id=$1 AND evidence_kind='order_attempt'", order['id'])
        self.assertEqual({'preflight_validated', 'submission_authorized'}, {row['evidence']['stage'] for row in stages})
        for row in stages:
            self.assertEqual({'home': 1, 'away': 0}, row['evidence']['score_baseline'])
            self.assertEqual({'received_at': projection['received_at'].astimezone(timezone.utc).isoformat(),
                              'source_updated_at': projection['source_updated_at'].astimezone(timezone.utc).isoformat(),
                              'source_time_quality': 'known'}, row['evidence']['fixture_projection'])
        self.assertNotIn(BET_PATH, [request.url.path for request in self.native_requests])

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
        self.assertIsNone(stored['settled_at'])
        self.assertIsNone(stored['provider_price'])
        self.assertIsNotNone(stored['receipt_observed_at'])
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_order_receipt_evidence WHERE order_id=$1',row['id']))

    async def test_worker_preserves_official_receipt_without_replacing_reservation(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        row=await self.reserve(signal)
        await self.pool.execute("UPDATE v2_bet_orders SET state='accepted',provider_order_no='EXACT' WHERE id=$1",row['id'])
        await self.api.post('/api/v1/accounts/leyu/refresh')
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        placed='2026-10-20T10:00:00+00:00'
        settled='2026-10-20T12:00:00+00:00'
        original=self.native
        def official(request):
            if request.url.path==ORDERS_PATH:
                state=json.loads(request.content)['orderStatus']
                return httpx.Response(200,json={'code':0,'data':{'total':1 if state==1 else 0,'records':[{
                    'orderNo':'EXACT','betAmount':'1.75','backAmount':'3.50','profitAmount':'1.75',
                    'oddFinally':'2.00','currencyCode':'CNY','betTime':placed,'settleTime':settled,
                    'orderStatus':'1','detailList':[{'betResult':4,'betStatus':'1','cancelType':0}]
                }] if state==1 else []}})
            return original(request)
        executor=Executor(self.worker_api,'https://platform.invalid/internal/executor',
            client_factory=lambda scope,session,**kw:AccountProtocolClient(scope,session,transport=httpx.MockTransport(official),**kw))
        await executor.account_job(work)
        stored=await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',row['id'])
        self.assertEqual('settled',stored['state'])
        self.assertEqual((Decimal('2'),Decimal('1.95')),(stored['stake'],stored['price']))
        self.assertEqual((Decimal('1.75'),Decimal('2.00'),Decimal('3.50'),'CNY'),
            (stored['provider_stake'],stored['provider_price'],stored['provider_payout'],stored['provider_currency']))
        self.assertEqual(datetime.fromisoformat(placed),stored['provider_placed_at'])
        self.assertEqual(datetime.fromisoformat(settled),stored['settled_at'])
        self.assertEqual('provider_reported_net',stored['pnl_basis'])
        self.assertIsNone(stored['provider_fees'])
        evidence=await self.pool.fetchrow('SELECT * FROM v2_order_receipt_evidence WHERE order_id=$1',row['id'])
        self.assertEqual('2.00',evidence['payload']['price'])
        self.assertEqual('CNY',evidence['payload']['currency'])
        self.assertEqual({'betResult':4,'betStatus':1,'orderStatus':1,'cancelType':0},
                         evidence['payload']['provider_settlement_codes'])
        visible=(await self.api.get('/api/v1/accounts/orders')).json()['items'][0]
        self.assertEqual(Decimal('2'),Decimal(str(visible['provider_price'])))
        self.assertNotIn(BET_PATH,[request.url.path for request in self.native_requests])

    async def test_settled_recheck_can_share_budget_with_recently_confirmed_open_orders(self):
        await self.healthy()
        signal_rows = [await self.signal() for _ in range(3)]
        await self.enable([row[1] for row in signal_rows], daily='8', exposure='8')
        unknown, open_order, settled = [await self.reserve(row[0]) for row in signal_rows]
        await self.pool.execute(
            "UPDATE v2_bet_orders SET state='unknown',provider_order_no='UNKNOWN' WHERE id=$1", unknown['id'])
        await self.pool.execute(
            "UPDATE v2_bet_orders SET state='accepted',provider_order_no='OPEN',"
            "receipt_observed_at=now()-interval '3 minutes',result_payload=$2 WHERE id=$1",
            open_order['id'], {'order_no': 'OPEN', 'state': 'unsettled'})
        await self.pool.execute(
            "UPDATE v2_bet_orders SET state='settled',provider_order_no='RECHECK',provider_price=1.95,"
            "provider_stake=2,provider_placed_at=now()-interval '2 hours',settled_net=1.90,"
            "settled_at=now()-interval '1 hour',receipt_observed_at=now()-interval '1 hour',"
            "updated_at=now()-interval '1 hour' WHERE id=$1", settled['id'])

        async def check_claim(expected, capable=True):
            self.assertEqual(202, (await self.api.post('/api/v1/accounts/leyu/refresh')).status_code)
            response = await self.worker_api.post('/internal/executor/claim', params={
                'settled_recheck': str(capable).lower()})
            self.assertEqual(200, response.status_code, response.text)
            work = response.json()
            self.assertEqual(expected, [row['provider_order_no'] for row in work['orders']])
            self.assertEqual(str(settled['id']) if 'RECHECK' in expected else None, work.get('settled_recheck'))
            result = await self.worker_api.post(f"/internal/executor/jobs/{work['job_id']}/finish", json={
                'attempt_id': work['attempt_id'], 'status': 'success', 'balance': '100',
                'observed_at': datetime.now(timezone.utc).isoformat()})
            self.assertEqual(200, result.status_code, result.text)

        await check_claim(['UNKNOWN', 'OPEN'])
        await self.pool.execute("UPDATE v2_bet_orders SET receipt_observed_at=now() WHERE id=$1", open_order['id'])
        await self.pool.execute(
            "UPDATE v2_fixtures SET phase='finished' WHERE id=(SELECT fixture_id FROM v2_expert_signals WHERE id=$1)",
            signal_rows[1][0])
        await check_claim(['UNKNOWN', 'OPEN'])
        await self.pool.execute(
            "UPDATE v2_fixtures SET phase='live' WHERE id=(SELECT fixture_id FROM v2_expert_signals WHERE id=$1)",
            signal_rows[1][0])
        await check_claim(['UNKNOWN', 'OPEN'], capable=False)
        await check_claim(['UNKNOWN', 'RECHECK'])
        await check_claim(['UNKNOWN', 'OPEN'])

    async def test_complete_history_can_append_a_verified_principal_refund(self):
        await self.healthy()
        signal, expert, _, _ = await self.signal()
        await self.enable([expert])
        order = await self.reserve(signal)
        order_id = order['id']
        now = datetime.now(timezone.utc)
        placed = now - timedelta(hours=2)
        first_settlement = now - timedelta(hours=1, minutes=5)
        first_observed = now - timedelta(hours=1)
        await self.pool.execute(
            "UPDATE v2_bet_orders SET state='accepted',provider_order_no='AMENDED',created_at=$2 WHERE id=$1",
            order_id, placed)

        async def claim():
            response = await self.api.post('/api/v1/accounts/leyu/refresh')
            self.assertEqual(202, response.status_code, response.text)
            response = await self.worker_api.post('/internal/executor/claim?settled_recheck=true')
            self.assertEqual(200, response.status_code, response.text)
            return response.json()

        winning_receipt = {'orderNo': 'AMENDED', 'betAmount': '2', 'backAmount': '3.60', 'profitAmount': '1.60',
                           'oddFinally': '1.80', 'betTime': placed.isoformat(), 'settleTime': first_settlement.isoformat(),
                           'orderStatus': 1, 'detailList': [{'betResult': 4, 'betStatus': 1, 'cancelType': 0}]}
        refund = winning_receipt | {
            'backAmount': '2.00', 'profitAmount': '0', 'settleTime': (first_observed + timedelta(minutes=5)).isoformat(),
            'orderStatus': 2, 'detailList': [{'betResult': 4, 'betStatus': 3, 'cancelType': 12}]}
        receipt, complete = winning_receipt, True

        def native(request):
            if request.url.path != ORDERS_PATH:
                return self.native(request)
            settled = json.loads(request.content)['orderStatus'] == 1
            total = (1 if complete else 2) if settled else 0
            return httpx.Response(200, json={'code': 0, 'data': {
                'total': total, 'records': [receipt] if settled else []}})

        transport = httpx.MockTransport(native)
        executor = Executor(
            self.worker_api, 'https://platform.invalid/internal/executor',
            client_factory=lambda scope, session, **kwargs: AccountProtocolClient(
                scope, session, transport=transport, **kwargs))
        work = await claim()
        self.assertNotIn('settled_recheck', work)
        await executor.account_job(work)
        current = await self.pool.fetchrow('SELECT settled_net,result_payload FROM v2_bet_orders WHERE id=$1', order_id)
        self.assertEqual(Decimal('1.60'), current['settled_net'])
        original = current['result_payload']
        await self.pool.execute('UPDATE v2_order_receipt_evidence SET observed_at=$2 WHERE order_id=$1', order_id,
                                first_observed)
        for receipt, complete in (
                (refund, False), (refund | {'settleTime': first_settlement.isoformat()}, True), (refund, True)):
            await self.pool.execute(
                "UPDATE v2_bet_orders SET updated_at=$2,receipt_observed_at=$2 WHERE id=$1", order_id, first_observed)
            await self.pool.execute("UPDATE v2_account_jobs SET updated_at=now()-interval '6 minutes'")
            work = await claim()
            self.assertEqual(str(order_id), work['settled_recheck'])
            await executor.account_job(work)
            current = await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1', order_id)
            expect_amendment = complete and receipt == refund
            self.assertEqual(Decimal('0') if expect_amendment else Decimal('1.60'), current['settled_net'])

        evidence = await self.pool.fetch(
            'SELECT payload FROM v2_order_receipt_evidence WHERE order_id=$1 ORDER BY observed_at', order_id)
        first, amended = [row['payload'] for row in evidence]
        self.assertEqual(original, first)
        self.assertEqual(current['result_payload'], amended)
        self.assertEqual(12, amended['provider_settlement_codes']['cancelType'])
        self.assertEqual((Decimal('2.00'), None, None),
                         (current['provider_payout'], current['provider_fees'], current['provider_currency']))
        await self.pool.execute("UPDATE v2_account_jobs SET updated_at=now()-interval '6 minutes'")
        self.assertNotIn('settled_recheck', await claim())
        self.assertNotIn(BET_PATH, [request.url.path for request in self.native_requests])

    async def test_history_rate_limit_preserves_fresh_balance_and_other_receipt(self):
        account=await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        first=await self.reserve(signal)
        second_signal,second_expert,_,_=await self.signal()
        await self.enable([expert,second_expert])
        second=await self.reserve(second_signal)
        for order,number in ((first,'SETTLED'),(second,'LIMITED')):
            await self.pool.execute("UPDATE v2_bet_orders SET state='accepted',provider_order_no=$2 WHERE id=$1",order['id'],number)
        policy=dict(await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1',self.user['id']))
        await self.pool.execute("UPDATE v2_leyu_accounts SET last_verified_at=now()-interval '12 hours' WHERE id=$1",account['id'])
        before=await self.pool.fetchval('SELECT last_verified_at FROM v2_leyu_accounts WHERE id=$1',account['id'])
        await self.api.post('/api/v1/accounts/leyu/refresh')
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        original=self.native
        calls=0
        limited=False
        def native(request):
            nonlocal calls
            if request.url.path==ORDERS_PATH:
                calls+=1
                if limited:
                    return httpx.Response(200,json={'code':'0403001','msg':'网关限流'})
                settled=json.loads(request.content)['orderStatus']==1
                return httpx.Response(200,json={'code':0,'data':{'total':1 if settled else 0,'records':[
                    {'orderNo':'SETTLED','betAmount':'2','profitAmount':'1.90'}] if settled else []}})
            return original(request)
        executor=Executor(self.worker_api,'https://platform.invalid/internal/executor',
            client_factory=lambda scope,session,**kw:AccountProtocolClient(scope,session,transport=httpx.MockTransport(native),**kw))
        await executor.account_job(work)
        self.assertEqual(2,calls,'two orders must share the same unsettled/settled history reads')
        settled=await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',first['id'])
        self.assertEqual(('settled',Decimal('0'),Decimal('1.90')),(settled['state'],settled['reserved_stake'],settled['settled_net']))
        # A later provider rejection remains isolated from the balance result.
        limited=True
        await self.api.post('/api/v1/accounts/leyu/refresh')
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        await executor.account_job(work)
        refreshed=await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE id=$1',account['id'])
        self.assertGreater(refreshed['last_verified_at'],before)
        self.assertLess((datetime.now(timezone.utc)-refreshed['balance_observed_at']).total_seconds(),10)
        self.assertEqual('healthy',refreshed['health_state'])
        job=await self.pool.fetchrow('SELECT * FROM v2_account_jobs WHERE id=$1',uuid.UUID(work['job_id']))
        self.assertEqual(('succeeded','order_reconciliation_partial'),(job['state'],job['last_error']))
        self.assertEqual([{'order_id':str(second['id']),'category':'upstream_rejected','provider_code':'0403001'}],job['details']['reconciliation_errors'])
        self.assertIn('部分历史订单',account_jobs.public_task(job,account['binding_revision'])['message'])
        retained=await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',second['id'])
        self.assertEqual(('accepted',second['reserved_stake'],None),(retained['state'],retained['reserved_stake'],retained['settled_net']))
        self.assertEqual(policy,dict(await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1',self.user['id'])))
        self.assertNotIn(BET_PATH,[request.url.path for request in self.native_requests])

    async def test_reconciliation_error_scope_and_lease_remain_enforced(self):
        account=await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        order=await self.reserve(signal)
        await self.pool.execute("UPDATE v2_bet_orders SET state='unknown',provider_order_no='EXACT' WHERE id=$1",order['id'])
        await self.api.post('/api/v1/accounts/leyu/refresh')
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        url=f"/internal/executor/jobs/{work['job_id']}/finish"
        error={'order_id':str(order['id']),'category':'upstream_rejected','provider_code':'0403001'}
        body={'attempt_id':work['attempt_id'],'status':'success','category':'order_reconciliation_partial',
              'balance':'100','observed_at':datetime.now(timezone.utc).isoformat(),'reconciliation_errors':[error]}
        for changes in (
            {'reconciliation_errors':[error|{'order_id':str(uuid.uuid4())}]},
            {'reconciliation_errors':[error,error]},
            {'scanned_orders':[str(order['id'])]},
            {'receipts':[{'order_no':'EXACT','state':'settled','net_profit':'2'}]},
            {'observed_at':(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()},
        ):
            response=await self.worker_api.post(url,json=body|changes)
            self.assertEqual(422,response.status_code,response.text)
        await self.pool.execute("UPDATE v2_account_jobs SET lease_until=now()-interval '1 second' WHERE id=$1",uuid.UUID(work['job_id']))
        self.assertEqual(409,(await self.worker_api.post(url,json=body)).status_code)
        stored=await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',order['id'])
        self.assertEqual(('unknown',order['reserved_stake']),(stored['state'],stored['reserved_stake']))

    async def test_history_failure_never_masks_final_balance_failure(self):
        account=await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        order=await self.reserve(signal)
        await self.pool.execute("UPDATE v2_bet_orders SET state='unknown',provider_order_no='EXACT' WHERE id=$1",order['id'])
        await self.pool.execute("UPDATE v2_leyu_accounts SET last_verified_at=now()-interval '12 hours' WHERE id=$1",account['id'])
        before=await self.pool.fetchval('SELECT last_verified_at FROM v2_leyu_accounts WHERE id=$1',account['id'])
        await self.api.post('/api/v1/accounts/leyu/refresh')
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        balance_calls=0
        original=self.native
        def native(request):
            nonlocal balance_calls
            if request.url.path==ORDERS_PATH:
                return httpx.Response(200,json={'code':'0403001'})
            if request.url.path==AMOUNT_PATH:
                balance_calls+=1
                if balance_calls>1:
                    return httpx.Response(503,json={'code':'503'})
            return original(request)
        executor=Executor(self.worker_api,'https://platform.invalid/internal/executor',
            client_factory=lambda scope,session,**kw:AccountProtocolClient(scope,session,transport=httpx.MockTransport(native),**kw))
        await executor.account_job(work)
        self.assertEqual(before,await self.pool.fetchval('SELECT last_verified_at FROM v2_leyu_accounts WHERE id=$1',account['id']))
        job=await self.pool.fetchrow('SELECT * FROM v2_account_jobs WHERE id=$1',uuid.UUID(work['job_id']))
        self.assertEqual(('failed','account_http_rejected'),(job['state'],job['last_error']))
        self.assertEqual(order['reserved_stake'],await self.pool.fetchval('SELECT reserved_stake FROM v2_bet_orders WHERE id=$1',order['id']))

    async def test_final_balance_failure_preserves_only_planned_settlement_receipts(self):
        account = await self.healthy()
        signal, expert, _, _ = await self.signal()
        await self.enable([expert])
        order = await self.reserve(signal)
        await self.pool.execute("UPDATE v2_bet_orders SET state='accepted',provider_order_no='EXACT' WHERE id=$1", order['id'])
        await self.api.post('/api/v1/accounts/leyu/refresh')
        work = (await self.worker_api.post('/internal/executor/claim')).json()
        url = f"/internal/executor/jobs/{work['job_id']}/finish"
        unexpected = await self.worker_api.post(url, json={
            'attempt_id': work['attempt_id'], 'status': 'failed', 'category': 'post_reconciliation_balance_failed',
            'receipts': [{'order_no': 'OUTSIDE-PLAN', 'state': 'settled', 'net_profit': '2'}],
        })
        self.assertEqual(422, unexpected.status_code, unexpected.text)
        balance_calls = 0

        def native(request):
            if request.url.path == ORDERS_PATH:
                rows = [{'orderNo': 'EXACT', 'betAmount': '2', 'backAmount': '4', 'profitAmount': '2'}]
                records = rows if json.loads(request.content)['orderStatus'] == 1 else []
                return httpx.Response(200, json={'code': 0, 'data': {'total': len(records), 'records': records}})
            if request.url.path == AMOUNT_PATH:
                nonlocal balance_calls
                balance_calls += 1
                if balance_calls == 2:
                    return httpx.Response(503, json={'code': '503'})
            self.assertNotEqual(BET_PATH, request.url.path)
            return self.native(request)

        executor = Executor(self.worker_api, 'https://platform.invalid/internal/executor',
            client_factory=lambda scope, session, **kw: AccountProtocolClient(
                scope, session, transport=httpx.MockTransport(native), **kw))
        original_finish = executor.finish
        executor.finish = AsyncMock(wraps=original_finish)
        await executor.account_job(work)
        self.assertEqual(2, balance_calls)
        body = executor.finish.await_args.args[1]
        stored = await self.pool.fetchrow('SELECT state,settled_net,reserved_stake FROM v2_bet_orders WHERE id=$1', order['id'])
        self.assertEqual(('settled', Decimal('2'), Decimal('0')), tuple(stored.values()))
        saved = await self.pool.fetchrow('SELECT * FROM v2_leyu_accounts WHERE id=$1', account['id'])
        for field in ('last_verified_at', 'balance_observed_at', 'available_balance', 'health_state'):
            self.assertEqual(account[field], saved[field])
        self.assertGreater(saved['renewal_retry_after'], datetime.now(timezone.utc))
        job = await self.pool.fetchrow('SELECT state,last_error FROM v2_account_jobs WHERE id=$1', uuid.UUID(work['job_id']))
        self.assertEqual(('failed', 'post_reconciliation_balance_failed'), tuple(job.values()))
        replay = await self.worker_api.post(url, json=body)
        self.assertEqual({'status': 'failed', 'idempotent': True}, replay.json())
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_order_receipt_evidence WHERE order_id=$1', order['id']))

    async def test_receipt_rejects_invalid_official_fields(self):
        from pydantic import ValidationError
        for extra in ({'price':'NaN'},{'price':'1'},{'stake':'-1'},{'settled_at':'2026-10-20T10:00:00'},{'currency':'usd'},
                      {'provider_settlement_codes':{'betResult':True}},
                      {'provider_settlement_codes':{'cancelType':'41'}},
                      {'provider_settlement_codes':{'provider_text':'unexpected'}}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                accounts.Receipt(order_no='O',state='settled',**extra)

    async def test_same_provider_account_cannot_be_bound_by_another_user(self):
        await self.bind()
        other=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,$2,'其他','hash','user')",other,f'{other}@test.invalid')
        self.user['id']=other
        await self.recent_session()
        response=await self.api.put('/api/v1/accounts/leyu',json={'account_label':'另一绑定','username':'OWN-USER','password':'password'})
        self.assertEqual(409,response.status_code)

    async def test_new_executor_reconciles_persisted_unknown_order_without_resubmitting(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        row=await self.reserve(signal)
        first=self.executor()
        work=await first.rpc('POST','/claim?live_capable=true')
        self.assertEqual('order',work['kind'])
        await first.order_job(work)
        await self.pool.execute("UPDATE v2_bet_orders SET state='unknown',updated_at=now()-interval '1 minute' WHERE id=$1",row['id'])
        self.assertEqual(1,sum(request.url.path==BET_PATH for request in self.native_requests))
        original=self.native
        def official(request):
            if request.url.path==ORDERS_PATH:
                self.native_requests.append(request)
                state=json.loads(request.content)['orderStatus']
                return httpx.Response(200,json={'code':0,'data':{'total':1 if state==1 else 0,'records':[
                    {'orderNo':'PROVIDER-ORDER','betAmount':'2','backAmount':'3.9','profitAmount':'1.9','currencyCode':'CNY'}
                ] if state==1 else []}})
            return original(request)
        restarted=self.executor()
        self.assertIsNot(first,restarted)
        restarted.client_factory=lambda scope,session,**kw:AccountProtocolClient(scope,session,transport=httpx.MockTransport(official),**kw)
        response=await self.api.post('/api/v1/accounts/leyu/refresh')
        self.assertEqual(202,response.status_code,response.text)
        recovery=await restarted.rpc('POST','/claim?live_capable=true')
        self.assertEqual('account',recovery['kind'])
        self.assertEqual([str(row['id'])],[item['id'] for item in recovery['orders']])
        await restarted.account_job(recovery)
        stored=await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',row['id'])
        self.assertEqual(('settled',Decimal('0'),Decimal('1.9')),(stored['state'],stored['reserved_stake'],stored['settled_net']))
        self.assertEqual('PROVIDER-ORDER',stored['provider_order_no'])
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_order_receipt_evidence WHERE order_id=$1',row['id']))
        self.assertEqual(1,sum(request.url.path==BET_PATH for request in self.native_requests))
        self.assertEqual('idle',(await restarted.rpc('POST','/claim?live_capable=true'))['kind'])

    async def test_restart_expired_submission_without_number_never_requeues_or_resubmits(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        row=await self.reserve(signal)
        attempt=uuid.uuid4()
        await self.pool.execute("UPDATE v2_bet_orders SET state='submitting',attempt_id=$2,lease_until=now()-interval '1 second' WHERE id=$1",row['id'],attempt)
        restarted=self.executor()
        for _ in range(3):
            self.assertEqual('idle',(await restarted.rpc('POST','/claim?live_capable=true'))['kind'])
            self.assertEqual(0,(await restarted.rpc('POST','/dispatch'))['reserved'])
        stored=await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',row['id'])
        self.assertEqual(('unknown',Decimal('2'),attempt),(stored['state'],stored['reserved_stake'],stored['attempt_id']))
        self.assertIsNone(stored['provider_order_no'])
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))
        self.assertEqual([],self.native_requests)

    async def test_receipt_replay_is_idempotent_but_conflicting_finish_cannot_replace_evidence(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        row=await self.reserve(signal)
        await self.pool.execute("UPDATE v2_bet_orders SET state='unknown',provider_order_no='IMMUTABLE' WHERE id=$1",row['id'])
        await self.api.post('/api/v1/accounts/leyu/refresh')
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        body={'attempt_id':work['attempt_id'],'status':'success','balance':'100',
              'observed_at':datetime.now(timezone.utc).isoformat(),'scanned_orders':[str(row['id'])],
              'receipts':[{'order_no':'IMMUTABLE','state':'settled','net_profit':'1.75','pnl_basis':'provider_reported_net',
                           'price':'2','stake':'1.75','payout':'3.5','currency':'CNY'}]}
        url=f"/internal/executor/jobs/{work['job_id']}/finish"
        finished=await self.worker_api.post(url,json=body)
        self.assertEqual(200,finished.status_code,finished.text)
        original=dict(await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',row['id']))
        evidence=dict(await self.pool.fetchrow('SELECT * FROM v2_order_receipt_evidence WHERE order_id=$1',row['id']))
        self.assertNotIn('provider_settlement_codes',evidence['payload'])
        legacy_document=accounts.JobFinish.model_validate(body).model_dump(
            mode='json', exclude={'complete_settled_recheck'})
        for key in ('provider_code','reconciliation_errors'):
            legacy_document.pop(key)
        legacy_document['receipts'][0]=evidence['payload']
        legacy_digest=hashlib.sha256(json.dumps(legacy_document,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.assertEqual(legacy_digest,await self.pool.fetchval(
            "SELECT details->>'finish_sha256' FROM v2_account_jobs WHERE id=$1",uuid.UUID(work['job_id'])))
        replay=await self.worker_api.post(url,json=body)
        self.assertEqual(200,replay.status_code,replay.text)
        self.assertTrue(replay.json()['idempotent'])
        conflict={**body,'receipts':[{**body['receipts'][0],'net_profit':'-1.75','payout':'0'}]}
        rejected=await self.worker_api.post(url,json=conflict)
        self.assertEqual(409,rejected.status_code,rejected.text)
        self.assertEqual(original,dict(await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',row['id'])))
        self.assertEqual([evidence],[dict(item) for item in await self.pool.fetch('SELECT * FROM v2_order_receipt_evidence WHERE order_id=$1',row['id'])])

    async def test_same_provider_order_number_is_isolated_by_owner_and_account_identity(self):
        first_account=await self.healthy()
        first_user=dict(self.user)
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        first_order=await self.reserve(signal)
        other=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,$2,'另一用户','hash','user')",other,f'{other}@test.invalid')
        self.user['id']=other
        await self.recent_session()
        second_account=await self.bind('second-own-user')
        session=encrypt_secret(b'k'*32,json.dumps({'leyu_api_base':'https://venue.invalid','leyu_request_id':'second-session'}))
        await self.pool.execute("UPDATE v2_leyu_accounts SET encrypted_session=$2,health_state='healthy',last_verified_at=now(),available_balance=100,balance_observed_at=now() WHERE id=$1",second_account['id'],session)
        await self.pool.execute("UPDATE v2_account_jobs SET state='succeeded' WHERE account_id=$1",second_account['id'])
        other_signal,other_expert,_,_=await self.signal()
        await self.enable([other_expert])
        second_order=await self.reserve(other_signal)
        self.assertNotEqual(first_account['provider_identity_hash'],second_account['provider_identity_hash'])
        await self.pool.execute("UPDATE v2_bet_orders SET state='unknown',provider_order_no='SAME-NUMBER' WHERE id=ANY($1::uuid[])",[first_order['id'],second_order['id']])
        self.user.update(first_user)
        await self.api.post('/api/v1/accounts/leyu/refresh')
        work=(await self.worker_api.post('/internal/executor/claim')).json()
        self.assertEqual([str(first_order['id'])],[item['id'] for item in work['orders']])
        body={'attempt_id':work['attempt_id'],'status':'success','balance':'100','observed_at':datetime.now(timezone.utc).isoformat(),
              'receipts':[{'order_no':'SAME-NUMBER','state':'settled','net_profit':'1.9','pnl_basis':'provider_reported_net','currency':'CNY'}],
              'scanned_orders':[str(second_order['id'])]}
        url=f"/internal/executor/jobs/{work['job_id']}/finish"
        self.assertEqual(422,(await self.worker_api.post(url,json=body)).status_code)
        body['scanned_orders']=[str(first_order['id'])]
        result=await self.worker_api.post(url,json=body)
        self.assertEqual(200,result.status_code,result.text)
        first_stored=await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',first_order['id'])
        second_stored=await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',second_order['id'])
        self.assertEqual(('settled',Decimal('1.9')),(first_stored['state'],first_stored['settled_net']))
        self.assertEqual(('unknown',Decimal('2'),None),(second_stored['state'],second_stored['reserved_stake'],second_stored['settled_net']))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_order_receipt_evidence WHERE order_id=$1',second_order['id']))
        visible=(await self.api.get('/api/v1/accounts/orders')).json()['items']
        self.assertEqual([str(first_order['id'])],[item['id'] for item in visible])

    async def test_missing_or_malformed_currency_never_invents_fees_or_missing_official_profit(self):
        await self.healthy()
        signals=[await self.signal(),await self.signal()]
        await self.enable([item[1] for item in signals])
        rows=[await self.reserve(item[0]) for item in signals]
        for row,number in zip(rows,('NO-CURRENCY','BAD-CURRENCY')):
            await self.pool.execute("UPDATE v2_bet_orders SET state='unknown',provider_order_no=$2 WHERE id=$1",row['id'],number)
        original=self.native
        def official(request):
            if request.url.path==ORDERS_PATH:
                state=json.loads(request.content)['orderStatus']
                return httpx.Response(200,json={'code':0,'data':{'total':2 if state==1 else 0,'records':[
                    {'orderNo':'NO-CURRENCY','profitAmount':'1.75','oddFinally':'2'},
                    {'orderNo':'BAD-CURRENCY','currencyCode':'usd','oddFinally':'2'}
                ] if state==1 else []}})
            return original(request)
        restarted=self.executor()
        restarted.client_factory=lambda scope,session,**kw:AccountProtocolClient(scope,session,transport=httpx.MockTransport(official),**kw)
        await self.api.post('/api/v1/accounts/leyu/refresh')
        work=await restarted.rpc('POST','/claim')
        self.assertEqual(2,len(work['orders']))
        await restarted.account_job(work)
        stored=await self.pool.fetch('SELECT * FROM v2_bet_orders ORDER BY provider_order_no')
        by_number={item['provider_order_no']:item for item in stored}
        for row in stored:
            self.assertEqual('settled',row['state'])
            self.assertIsNone(row['provider_currency'])
            self.assertIsNone(row['provider_fees'])
            self.assertIsNone(row['provider_stake'])
            self.assertIsNone(row['provider_payout'])
            self.assertEqual((Decimal('2'),Decimal('1.95')),(row['stake'],row['price']))
        self.assertEqual((Decimal('1.75'),'provider_reported_net'),(by_number['NO-CURRENCY']['settled_net'],by_number['NO-CURRENCY']['pnl_basis']))
        self.assertEqual((None,'unknown'),(by_number['BAD-CURRENCY']['settled_net'],by_number['BAD-CURRENCY']['pnl_basis']))
        self.assertEqual(2,await self.pool.fetchval('SELECT count(*) FROM v2_order_receipt_evidence'))
        self.assertNotIn(BET_PATH,[request.url.path for request in self.native_requests])

    async def test_manual_bet_uses_own_saved_stake_without_auto_betting_authorization(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        await self.pool.execute('UPDATE v2_user_bet_settings SET auto_enabled=FALSE WHERE user_id=$1',self.user['id'])
        configured=await self.api.put('/api/v1/accounts/leyu/manual-betting',json={'stake_per_order':'3','daily_limit':'4','open_exposure_limit':'4','daily_timezone':'Asia/Shanghai'})
        self.assertEqual(200,configured.status_code,configured.text)
        settings=(await self.api.get('/api/v1/accounts/leyu/manual-betting')).json()
        self.assertEqual((Decimal('3'),True,True,True),(Decimal(settings['stake_per_order']),settings['enabled'],settings['account_ready'],settings['entitlement']))
        first=await self.api.post('/api/v1/accounts/leyu/manual-betting/orders',json={'signal_id':str(signal)})
        self.assertEqual(202,first.status_code,first.text)
        order=first.json()['order']
        self.assertEqual((Decimal('3'),True), (Decimal(order['stake']),order['manual']))
        repeat=await self.api.post('/api/v1/accounts/leyu/manual-betting/orders',json={'signal_id':str(signal)})
        self.assertEqual(order['id'],repeat.json()['order']['id'],'重复点击不得重复占用敞口')
        stored=await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',uuid.UUID(order['id']))
        self.assertEqual((True,Decimal('3'),Decimal('3')),(stored['manual'],stored['stake'],stored['reserved_stake']))
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders WHERE user_id=$1',self.user['id']))
        self.assertFalse(await self.pool.fetchval('SELECT auto_enabled FROM v2_user_bet_settings WHERE user_id=$1',self.user['id']),'手动下注不得改写自动投注授权')

    async def test_manual_bet_respects_limits_and_membership(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        no_stake=await self.api.post('/api/v1/accounts/leyu/manual-betting/orders',json={'signal_id':str(signal)})
        self.assertEqual(409,no_stake.status_code,no_stake.text)
        over=await self.api.put('/api/v1/accounts/leyu/manual-betting',json={'stake_per_order':'5','daily_limit':'4','open_exposure_limit':'4','daily_timezone':'Asia/Shanghai'})
        self.assertEqual(422,over.status_code,over.text)
        unknown_tz=await self.api.put('/api/v1/accounts/leyu/manual-betting',json={'stake_per_order':'2','daily_limit':'4','open_exposure_limit':'4','daily_timezone':'Not/AZone'})
        self.assertEqual(422,unknown_tz.status_code,unknown_tz.text)
        self.assertEqual(200,(await self.api.put('/api/v1/accounts/leyu/manual-betting',json={'stake_per_order':'2','daily_limit':'2','open_exposure_limit':'2','daily_timezone':'Asia/Shanghai'})).status_code)
        first=await self.api.post('/api/v1/accounts/leyu/manual-betting/orders',json={'signal_id':str(signal)})
        self.assertEqual(202,first.status_code,first.text)
        second_signal,*_=await self.signal()
        full=await self.api.post('/api/v1/accounts/leyu/manual-betting/orders',json={'signal_id':str(second_signal)})
        self.assertEqual(409,full.status_code,'日限额与开放敞口用满后不得继续下注')
        row=await self.pool.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1',uuid.UUID(first.json()['order']['id']))
        self.assertEqual(200,(await self.api.put('/api/v1/accounts/leyu/manual-betting',json={'stake_per_order':'2','daily_limit':'4','open_exposure_limit':'4','daily_timezone':'Asia/Shanghai'})).status_code)
        async with self.pool.acquire() as conn:
            with self.assertRaises(orders.HTTPException):
                await orders.recheck_order(conn,row,self.settings)
        await self.pool.execute('DELETE FROM v2_memberships WHERE user_id=$1',self.user['id'])
        expired=await self.api.post('/api/v1/accounts/leyu/manual-betting/orders',json={'signal_id':str(second_signal)})
        self.assertEqual(403,expired.status_code,'手动下注不得绕过会员实盘权益')

    async def test_manual_bet_rearm_requires_explicit_confirmation(self):
        await self.healthy()
        self.settings.live_orders_enabled=True
        await self.pool.execute("INSERT INTO v2_admin_settings(setting_key,encrypted_value) VALUES('auto_betting_enabled',$1) ON CONFLICT(setting_key) DO UPDATE SET encrypted_value=EXCLUDED.encrypted_value",encrypt_secret(b'k'*32,'true'))
        plan=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_membership_plans(id,code,name,price,duration_days,enabled,entitlements) VALUES($1,$2,'VIP',10,30,TRUE,$3)",plan,str(plan),{'automatic_betting':True})
        await self.pool.execute("INSERT INTO v2_memberships(id,user_id,plan_id,status,starts_at,ends_at,grant_source) VALUES($1,$2,$3,'active',now()-interval '1 minute',now()+interval '1 day','test')",uuid.uuid4(),self.user['id'],plan)
        body={'stake_per_order':'2','daily_limit':'4','open_exposure_limit':'4','daily_timezone':'Asia/Shanghai'}
        await self.pool.execute("INSERT INTO v2_user_bet_settings(user_id,auto_enabled,emergency_stopped) VALUES($1,FALSE,FALSE) ON CONFLICT(user_id) DO UPDATE SET emergency_stopped=FALSE",self.user['id'])
        first=await self.api.put('/api/v1/accounts/leyu/manual-betting',json=body)
        self.assertEqual(200,first.status_code,first.text)
        self.assertEqual(200,(await self.api.post('/api/v1/accounts/leyu/emergency-stop')).status_code)
        self.assertTrue((await self.api.get('/api/v1/accounts/leyu/manual-betting')).json()['emergency_stopped'])
        refused=await self.api.put('/api/v1/accounts/leyu/manual-betting',json=body)
        self.assertEqual(403,refused.status_code,'紧急停止后不得仅凭保存金额恢复')
        self.assertTrue((await self.api.get('/api/v1/accounts/leyu/manual-betting')).json()['emergency_stopped'])
        self.assertEqual(200,(await self.api.put('/api/v1/accounts/leyu/manual-betting',json={**body,'confirmation_phrase':'I_UNDERSTAND'})).status_code)
        self.assertFalse((await self.api.get('/api/v1/accounts/leyu/manual-betting')).json()['emergency_stopped'])


if __name__=='__main__':
    unittest.main()
