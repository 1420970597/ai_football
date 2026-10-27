"""Runtime config and revocation boundaries: explicit temporary PostgreSQL only."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from app import auth
from app.routes import admin, internal
from app.runtime_settings import runtime_quote_max_age
from app.security import encrypt_secret, token_hash
import test_identity as support


@unittest.skipUnless(support.TEST_URL, 'requires isolated database')
class IdentityRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = support.IdentityIntegrationTests.asyncTearDown
    register = support.IdentityIntegrationTests.register
    admin_login = support.IdentityIntegrationTests.admin_login
    csrf = support.IdentityIntegrationTests.csrf

    async def asyncSetUp(self):
        await support.IdentityIntegrationTests.asyncSetUp(self)
        self.app.include_router(internal.router)
        self.app.state.settings.hermes_api_key='internal-worker-token'
        self.app.state.settings.hermes_provider='custom:cline'
        self.app.state.settings.quote_max_age_seconds=15
        @self.app.middleware('http')
        async def request_id(request, call_next):
            request.state.request_id='runtime-test-request'
            return await call_next(request)

    async def test_saved_config_is_used_next_request_and_internal_scope_is_minimal(self):
        self.assertEqual(401,(await self.client.get('/internal/review/config')).status_code)
        await self.admin_login()
        invalid=await self.client.put('/api/v1/admin/settings',headers=self.csrf(True),
            json={'values':{'quote_max_age_seconds':'0'}})
        self.assertEqual(422,invalid.status_code)
        response=await self.client.put('/api/v1/admin/settings',headers=self.csrf(True),json={'values':{
            'hermes_analysis_model':'cline-pass/deepseek-v4.1-flash','quote_max_age_seconds':'90'}})
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(90,await runtime_quote_max_age(self.app.state.settings))
        config=await self.client.get('/internal/review/config',headers={'Authorization':'Bearer internal-worker-token'})
        self.assertEqual(200,config.status_code,config.text)
        self.assertEqual({'model','provider','quote_max_age_seconds','configuration_version'},set(config.json()))
        self.assertEqual('cline-pass/deepseek-v4.1-flash',config.json()['model'])
        row=await self.pool.fetchrow("SELECT request_id,details FROM v2_audit_log WHERE action='settings_updated' ORDER BY created_at DESC LIMIT 1")
        self.assertEqual('runtime-test-request',row['request_id'])
        self.assertNotIn('internal-worker-token',str(row))

    async def test_unreadable_setting_is_not_presented_as_absent_or_overwritten(self):
        await self.admin_login()
        await self.pool.execute("INSERT INTO v2_admin_settings(setting_key,encrypted_value) VALUES('quote_max_age_seconds','broken-cipher')")
        with patch.object(admin,'collector_health',AsyncMock(return_value={'state':'unavailable'})):
            response=await self.client.get('/api/v1/admin/settings')
        state=response.json()['items']['quote_max_age_seconds']
        self.assertTrue(state['configured']);self.assertEqual('unreadable',state['state'])
        config=await self.client.get('/internal/review/config',headers={'Authorization':'Bearer internal-worker-token'})
        self.assertEqual(503,config.status_code)
        self.assertEqual('broken-cipher',await self.pool.fetchval("SELECT encrypted_value FROM v2_admin_settings WHERE setting_key='quote_max_age_seconds'"))

    async def test_registration_uses_normalized_account_and_network_buckets(self):
        with patch.object(auth,'_limit',AsyncMock()) as limit:
            result=await self.client.post('/api/v1/auth/register',json={
                'email':'Mixed@Example.Invalid','display_name':'测试用户','password':support.PASSWORD})
        self.assertEqual(201,result.status_code)
        self.assertEqual(('register','mixed@example.invalid'),limit.await_args.args[1:])
        request=SimpleNamespace(client=SimpleNamespace(host='192.0.2.10'))
        for _ in range(5): await auth._limit(request,'register','normalized@example.invalid',maximum=5)
        request.client.host='192.0.2.20'
        with self.assertRaises(Exception) as caught:
            await auth._limit(request,'register','normalized@example.invalid',maximum=5)
        self.assertEqual(429,caught.exception.status_code)

    async def test_all_sensitive_admin_writes_recheck_revocation_after_entry(self):
        user=await self.register();uid=uuid.UUID(user['user']['id'])
        await self.pool.execute("INSERT INTO v2_membership_plans(id,code,name,price,duration_days,entitlements) VALUES($1,'runtimevip','测试套餐',20,30,'{}')",uuid.uuid4())
        plan=await self.pool.fetchrow("SELECT id,code FROM v2_membership_plans WHERE code='runtimevip'")
        membership=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_memberships(id,user_id,plan_id,status,starts_at,ends_at,grant_source) VALUES($1,$2,$3,'active',now(),now()+interval '1 day','admin')",membership,uid,plan['id'])
        requests=[('PUT','/api/v1/admin/settings',{'values':{'auto_betting_enabled':'true'}}),
            ('PUT','/api/v1/admin/membership/plans/newplan',{'code':'newplan','name':'新套餐','price':'20','duration_days':30,'entitlements':{}}),
            ('POST','/api/v1/admin/membership/grants',{'email':'user@example.invalid','plan_code':plan['code'],'days':5,'reason':'隔离测试开通','idempotency_key':str(uuid.uuid4())}),
            ('POST',f'/api/v1/admin/membership/grants/{membership}/revoke',{'reason':'隔离测试撤销'})]
        original=admin.require_admin
        for index,(method,path,body) in enumerate(requests):
            token=f'isolated-admin-{index}';csrf=f'isolated-csrf-{index}'
            await self.pool.execute("INSERT INTO v2_sessions(token_hash,csrf_hash,user_id,session_kind,expires_at,reauthenticated_at) VALUES($1,$2,$3,'admin',now()+interval '1 hour',now())",token_hash(token),token_hash(csrf),self.admin_id)
            self.client.cookies.set(auth.ADMIN_COOKIE,token);self.client.cookies.set(auth.ADMIN_CSRF_COOKIE,csrf)
            async def revoked(request):
                identity=await original(request)
                await self.pool.execute('DELETE FROM v2_sessions WHERE token_hash=$1',identity['_token_hash'])
                return identity
            with patch.object(admin,'require_admin',side_effect=revoked):
                response=await self.client.request(method,path,headers={'X-CSRF-Token':csrf},json=body)
            self.assertEqual(401,response.status_code,(path,response.text))
        self.assertEqual(0,await self.pool.fetchval("SELECT count(*) FROM v2_admin_settings"))
        self.assertEqual(0,await self.pool.fetchval("SELECT count(*) FROM v2_membership_plans WHERE code='newplan'"))
        self.assertEqual('active',await self.pool.fetchval('SELECT status FROM v2_memberships WHERE id=$1',membership))

    async def test_plan_audit_records_public_diff_and_request_identity(self):
        await self.admin_login()
        body={'code':'auditplan','name':'审计套餐','price':'25.00','duration_days':30,'entitlements':{'expert_full_history':True}}
        for price in ('25.00','35.00'):
            body['price']=price
            response=await self.client.put('/api/v1/admin/membership/plans/auditplan',headers=self.csrf(True),json=body)
            self.assertEqual(200,response.status_code,response.text)
        audit=await self.pool.fetchrow("SELECT request_id,details FROM v2_audit_log WHERE action='membership_plan_updated' ORDER BY created_at DESC LIMIT 1")
        self.assertEqual('runtime-test-request',audit['request_id'])
        self.assertEqual('25.00',audit['details']['before']['price'])
        self.assertEqual('35.00',audit['details']['after']['price'])
