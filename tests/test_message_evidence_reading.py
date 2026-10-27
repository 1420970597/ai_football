"""真实临时PG的私有原文/轨迹阅读：分页、权限、完整快照和去敏。"""
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch
import uuid

from app import auth, message_read
from app.routes import expert_content, public, telegram
from app.review_tool_evidence import encode_snapshot
from app.security import token_hash
import test_admin_reviews as support


@unittest.skipUnless(support.ADMIN_URL, 'requires explicit isolated PostgreSQL')
class MessageEvidenceReadingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.AdminReviewTests.asyncSetUp
    asyncTearDown = support.AdminReviewTests.asyncTearDown
    add_job = support.AdminReviewTests.add_job

    async def prepare(self):
        for router in (public.router, expert_content.router, telegram.router):
            self.app.include_router(router)
        self.app.state.settings.encryption_key = b't'*32
        plan = uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_membership_plans(id,code,name,duration_days,entitlements,enabled) VALUES($1,'read-test','隔离会员',30,$2,true)",
                                plan, {'expert_full_history': True})
        await self.pool.execute("INSERT INTO v2_memberships(id,user_id,plan_id,status,starts_at,ends_at,grant_source) VALUES($1,$2,$3,'active',now()-interval '1 day',now()+interval '1 day','test')",
                                uuid.uuid4(), self.user, plan)
        self.client.headers['Authorization'] = 'Bearer isolated-user'
        self.client.headers['X-Session-Context'] = 'user'
        self.now = datetime.now(timezone.utc)

    async def append_trace(self, job, sequence, kind='model_output', payload=None, created=None):
        identity = uuid.uuid4()
        await self.pool.execute('INSERT INTO v2_analysis_traces(id,job_id,sequence_no,trace_kind,payload,created_at) VALUES($1,$2,$3,$4,$5,$6)',
            identity, job, sequence, kind, payload if payload is not None else {'content': '完整输出-' + str(sequence)}, created or self.now)
        return identity

    async def path(self):
        return '/api/v1/public/messages/' + str(self.message)

    async def test_multi_job_attempt_order_pages_latest_summary_and_full_text_never_truncated(self):
        await self.prepare()
        await self.pool.execute('DELETE FROM v2_analysis_traces')
        same = self.now - timedelta(minutes=1)
        job2 = uuid.uuid4()
        await self.pool.execute("UPDATE v2_analysis_jobs SET created_at=$2 WHERE id=$1", self.job, same)
        await self.pool.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,created_at) VALUES($1,$2,'succeeded',$3,$4)",
                                job2, self.message, str(job2), same)
        ids = []
        for job in sorted((self.job, job2)):
            for number in range(1, 35):
                ids.append(await self.append_trace(job, number, payload={'content': ('模型原文'*10000 if number == 34 else str(number)), 'attempt_id':str(uuid.uuid4())}))
        latest = await self.append_trace(max(self.job, job2), 35, 'system_review_result', {'content':'系统最新中文确认，不依赖第一页'})
        detail = await self.client.get(await self.path())
        self.assertEqual(200, detail.status_code, detail.text)
        self.assertEqual('private, no-store', detail.headers['cache-control'])
        data = detail.json()
        self.assertEqual(30, len(data['analysis']))
        self.assertEqual(str(latest), data['system_summary']['id'])
        self.assertTrue(data['analysis_has_more'])
        seen = [row['id'] for row in data['analysis']]
        cursor = data['analysis_next_cursor']
        full = []
        while cursor:
            response = await self.client.get((await self.path())+'/traces', params={'cursor':cursor})
            self.assertEqual(200, response.status_code, response.text)
            page = response.json()
            seen.extend(row['id'] for row in page['items'])
            full.extend(row['payload'].get('content','') for row in page['items'])
            cursor = page['next_cursor']
        self.assertEqual(list(map(str, ids))+[str(latest)], seen)
        self.assertEqual(69, len(set(seen)))
        self.assertIn('模型原文'*10000, full)

    async def test_cursor_rejects_cross_message_identity_session_context_expiry_and_changed_limit(self):
        await self.prepare()
        for number in range(2, 34):
            await self.append_trace(self.job, number)
        original = await self.client.get(await self.path())
        cursor = original.json()['analysis_next_cursor']
        other, _ = await self.add_job()
        response = await self.client.get('/api/v1/public/messages/'+str(other)+'/traces',params={'cursor':cursor})
        self.assertEqual(400,response.status_code)
        response = await self.client.get((await self.path())+'/traces',params={'cursor':cursor,'limit':100})
        self.assertEqual(400,response.status_code)
        await self.pool.execute("INSERT INTO v2_sessions(token_hash,csrf_hash,user_id,session_kind,expires_at) VALUES($1,$2,$3,'user',now()+interval '1 hour')",
                                token_hash('same-user-new-session'),token_hash('csrf'),self.user)
        for token, context in (('same-user-new-session','user'),('isolated-admin-one','admin')):
            response = await self.client.get((await self.path())+'/traces',params={'cursor':cursor},
                headers={'Authorization':'Bearer '+token,'X-Session-Context':context})
            self.assertEqual(400,response.status_code,response.text)
        with patch('app.message_read.time.time', return_value=__import__('time').time()+901):
            response = await self.client.get((await self.path())+'/traces',params={'cursor':cursor})
            self.assertEqual(400,response.status_code)
        await self.pool.execute("UPDATE v2_memberships SET ends_at=now()-interval '1 second' WHERE user_id=$1",self.user)
        response = await self.client.get((await self.path())+'/traces',params={'cursor':cursor})
        self.assertEqual(403,response.status_code)
        self.assertEqual('private, no-store',response.headers['cache-control'])

    async def test_public_and_signal_details_hide_media_headers_inline_images_and_chunk_bytes(self):
        await self.prepare()
        await self.pool.execute('UPDATE v2_expert_messages SET media_objects=$2,reply_context=$3 WHERE id=$1', self.message,
            [{'object_key':'private-blob-secret','bucket':'private-bucket','headers':{'Authorization':'Bearer private'},'mime_type':'image/png','url':'https://private.invalid'}],
            {'raw_text':'真实回复原文','headers':{'Authorization':'Bearer reply-secret'}})
        await self.append_trace(self.job,2,'hermes_request',{'headers':{'Authorization':'Bearer header-secret'},'content':'合法输入正文',
            'embedded':'data:image/png;base64,aW1hZ2U=', 'json':'{"object_key":"json-secret","text":"完整模型输入"}'})
        await self.append_trace(self.job,3,'tool_response_chunk',{'call_id':'call','data_base64':'encoded-blob-secret','chunk_index':0})
        await self.pool.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) VALUES('read-fixture','read-fixture','football','测试联赛','甲','乙',now(),'live')")
        signal=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,market_kind,line,selection,recommendation_price,quote_time,confidence) VALUES($1,$2,$3,'read-fixture','live','winner','独赢','winner',NULL,'home',1.95,now(),.8)",signal,self.message,self.expert)
        for path in (await self.path(),'/api/v1/public/signals/'+str(signal)):
            response=await self.client.get(path)
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual('private, no-store',response.headers['cache-control'])
            for secret in ('private-blob-secret','private-bucket','header-secret','reply-secret','json-secret','encoded-blob-secret','aW1hZ2U='):
                self.assertNotIn(secret,response.text)
            self.assertIn('合法输入正文',response.text)
        stored=await self.pool.fetchval("SELECT payload->>'data_base64' FROM v2_analysis_traces WHERE job_id=$1 AND sequence_no=3",self.job)
        self.assertEqual('encoded-blob-secret',stored)

    async def test_member_full_tool_replay_has_all_items_and_cross_message_or_corruption_rejected(self):
        await self.prepare()
        result={'search_key':'read-key','search_complete':True,'next_cursor':None,'total_candidates':100,
                'items':[{'id':str(index),'home_team':'主'+str(index)+'原始队名'*100,'away_team':'客'+str(index)+'完整候选'*100} for index in range(100)]}
        manifest,chunks=encode_snapshot(result)
        self.assertGreater(len(chunks),1)
        manifest['call_id']='read-call'
        for index,data in enumerate(chunks):
            await self.append_trace(self.job,index+2,'tool_response_chunk',{'call_id':'read-call','chunk_index':index,
                'snapshot_sha256':manifest['sha256'],'data_base64':data})
        identity=await self.append_trace(self.job,len(chunks)+2,'mcp_search_matches',{'response_snapshot':manifest})
        first=await self.client.get(await self.path())
        self.assertEqual(200,first.status_code,first.text)
        self.assertNotIn('data_base64',first.text)
        response=await self.client.get((await self.path())+'/tools/'+str(identity))
        self.assertEqual(200,response.status_code,response.text)
        self.assertTrue(response.json()['complete'])
        self.assertEqual(result,response.json()['result'])
        await self.pool.execute("UPDATE v2_memberships SET ends_at=now()-interval '1 second' WHERE user_id=$1",self.user)
        forbidden=await self.client.get((await self.path())+'/tools/'+str(identity))
        self.assertEqual(403,forbidden.status_code)
        self.assertEqual('private, no-store',forbidden.headers['cache-control'])
        await self.pool.execute("UPDATE v2_memberships SET ends_at=now()+interval '1 day' WHERE user_id=$1",self.user)
        other,_=await self.add_job()
        response=await self.client.get('/api/v1/public/messages/'+str(other)+'/tools/'+str(identity))
        self.assertEqual(404,response.status_code)
        await self.pool.execute("UPDATE v2_analysis_traces SET payload=jsonb_set(payload,'{data_base64}','\"corrupt\"') WHERE job_id=$1 AND trace_kind='tool_response_chunk'",self.job)
        response=await self.client.get((await self.path())+'/tools/'+str(identity))
        self.assertEqual(409,response.status_code,response.text)
        self.assertNotIn('items',response.json())

    async def test_admin_dual_cookie_media_uses_admin_context_and_revocation_rechecks(self):
        await self.prepare()
        self.client.headers.pop('Authorization',None)
        self.client.cookies.set(auth.USER_COOKIE,'isolated-user')
        self.client.cookies.set(auth.ADMIN_COOKIE,'isolated-admin-one')
        await self.pool.execute("UPDATE v2_memberships SET ends_at=now()-interval '1 second' WHERE user_id=$1",self.user)
        path='/api/v1/admin/telegram/messages/'+str(self.message)
        response=await self.client.get(path)
        self.assertEqual(200,response.status_code,response.text)
        self.assertTrue(response.json()['message']['media_objects'][0]['url'].startswith(path+'/media/'))
        with patch('app.routes.expert_content.read_image',return_value=support.IMAGE):
            image=await self.client.get(path+'/media/0')
        self.assertEqual(200,image.status_code,image.text)
        self.assertEqual(support.IMAGE,image.content)
        self.assertEqual('private, no-store',image.headers['cache-control'])
        self.assertEqual(403,(await self.client.get(await self.path())).status_code)
        await self.pool.execute('DELETE FROM v2_sessions WHERE user_id=$1',self.admin)
        response=await self.client.get(path+'/traces')
        self.assertEqual(401,response.status_code)
        self.assertEqual('private, no-store',response.headers['cache-control'])

    async def test_101_signal_bindings_are_explicitly_paged_without_silent_cap(self):
        await self.prepare()
        await self.pool.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) VALUES('read-fixture','read-fixture','football','测试联赛','甲','乙',now(),'scheduled')")
        for number in range(101):
            await self.pool.execute("INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,market_kind,line,selection,recommendation_price,quote_time,confidence) VALUES($1,$2,$3,'read-fixture','scheduled',$4,'独赢','winner',NULL,'home',1.95,now(),.8)",uuid.uuid4(),self.message,self.expert,str(number))
        detail=(await self.client.get(await self.path())).json()
        self.assertEqual(30,len(detail['signals']))
        self.assertTrue(detail['signals_has_more'])
        seen={item['id'] for item in detail['signals']}
        cursor=detail['signals_next_cursor']
        while cursor:
            page=await self.client.get((await self.path())+'/signals',params={'cursor':cursor})
            self.assertEqual(200,page.status_code,page.text)
            seen.update(item['id'] for item in page.json()['items'])
            cursor=page.json()['next_cursor']
        self.assertEqual(101,len(seen))
        anonymous=await self.client.get((await self.path())+'/signals',headers={'Authorization':'Bearer invalid'})
        self.assertEqual(403,anonymous.status_code)

    async def test_100_ceiling_tampering_and_anonymous_endpoints_are_enforced(self):
        await self.prepare()
        for number in range(2,105):
            await self.append_trace(self.job,number)
        first=await self.client.get((await self.path())+'/traces',params={'limit':100})
        self.assertEqual(200,first.status_code,first.text)
        self.assertEqual(100,len(first.json()['items']))
        cursor=first.json()['next_cursor']
        last=await self.client.get((await self.path())+'/traces',params={'limit':100,'cursor':cursor})
        self.assertEqual(4,len(last.json()['items']))
        self.assertIsNone(last.json()['next_cursor'])
        bad=await self.client.get((await self.path())+'/traces',params={'limit':100,'cursor':cursor[:-8]+'AAAAAAAA'})
        self.assertEqual(400,bad.status_code)
        for limit in (0,101):
            bad=await self.client.get((await self.path())+'/traces',params={'limit':limit})
            self.assertEqual(422,bad.status_code)
        for ending in ('','/traces','/signals','/media/0','/tools/'+str(uuid.uuid4())):
            forbidden=await self.client.get((await self.path())+ending,headers={'Authorization':'Bearer invalid'})
            self.assertEqual(403,forbidden.status_code,forbidden.text)
            self.assertEqual('private, no-store',forbidden.headers['cache-control'])
