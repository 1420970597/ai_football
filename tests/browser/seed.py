"""只允许临时 web_integration 数据库的人造网页联调证据。"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import sys
import uuid
import asyncpg

sys.path.insert(0, '/app')
from app.security import encrypt_secret, hash_password

EXPERT = uuid.UUID('11111111-1111-4111-8111-111111111111')
SOURCE = uuid.UUID('55555555-5555-4555-8555-555555555555')
MESSAGE = uuid.UUID('22222222-2222-4222-8222-222222222222')
UNRESOLVED = uuid.UUID('66666666-6666-4666-8666-666666666666')
PLAN = uuid.UUID('77777777-7777-4777-8777-777777777777')
JOB = uuid.UUID('88888888-8888-4888-8888-888888888888')
async def seed():
    url=os.environ['DATABASE_URL']
    if not url.endswith('/web_integration') or '@pg:' not in url:
        raise RuntimeError('refusing a non-isolated database')
    conn=await asyncpg.connect(url)
    await conn.set_type_codec('jsonb',encoder=json.dumps,decoder=json.loads,schema='pg_catalog')
    now=datetime.now(timezone.utc)
    try:
        async with conn.transaction():
            await conn.execute("INSERT INTO v2_membership_plans(id,code,name,price,duration_days,entitlements,enabled) VALUES($1,'webvip','隔离专家会员',88,30,$2,TRUE)",PLAN,{'expert_full_history':True,'expert_live_signals':True})
            free_plan = await conn.fetchval("SELECT id FROM v2_membership_plans WHERE code='free' AND enabled")
            if not free_plan:
                raise RuntimeError('isolated role matrix requires the migrated free plan')
            password_hash = hash_password('test-only-role-matrix-password-1234')
            for index, role in enumerate(('ordinary', 'expired', 'valid'), start=1):
                user_id = uuid.UUID(f'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa{index}')
                await conn.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,$2,$3,$4,'user')", user_id, f'{role}@role-matrix.invalid', f'权限矩阵{role}', password_hash)
                await conn.execute("INSERT INTO v2_memberships(id,user_id,plan_id,status,starts_at,ends_at,grant_source) VALUES($1,$2,$3,'active',$4,$5,'isolated_browser_fixture')", uuid.uuid4(), user_id, free_plan, now-timedelta(days=60), now+timedelta(days=36500))
                if role != 'ordinary':
                    # Keep the expired row active: access must fail because of the real end time.
                    await conn.execute("INSERT INTO v2_memberships(id,user_id,plan_id,status,starts_at,ends_at,grant_source) VALUES($1,$2,$3,'active',$4,$5,'isolated_browser_fixture')", uuid.uuid4(), user_id, PLAN, now-timedelta(days=31), now+timedelta(days=1) if role == 'valid' else now-timedelta(days=1))
                credentials = encrypt_secret(bytes.fromhex(os.environ['DATA_ENCRYPTION_KEY']), json.dumps({'username': f'isolated-{role}', 'password': 'never-a-provider-credential'}))
                await conn.execute("INSERT INTO v2_leyu_accounts(id,user_id,encrypted_credentials,account_label) VALUES($1,$2,$3,$4)", uuid.UUID(f'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb{index}'), user_id, credentials, f'隔离绑定-{role}')
            await conn.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'-1009999999','隔离样本群')",SOURCE)
            await conn.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'999999','隔离专家林老师')",EXPERT)
            await conn.execute("INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)",SOURCE,EXPERT)
            for idx,(phase,league,home,away) in enumerate([('live','测试联赛甲','测试主队甲','测试客队甲'),('scheduled','测试联赛乙','测试主队乙','测试客队乙'),('finished','测试联赛甲','测试主队丙','测试客队丙'),('cancelled','测试联赛乙','测试主队丁','测试客队丁')]):
                fixture=f'web-integration-match-{idx}'
                await conn.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_id,competition_name,home_team,away_team,kickoff_at,phase,home_score,away_score,clock_minute,source_updated_at,source_time_quality) VALUES($1,$1,'football',$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'provided')",fixture,f'league-{idx%2}',league,home,away,now,phase,1 if phase=='live' else None,0 if phase=='live' else None,65 if phase=='live' else None,now)
                if idx<2:
                    for selection,price in [('home','1.96'),('away','1.92')]:
                        await conn.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,period,market_kind,line,selection,price,status,is_main,source_updated_at,source_time_quality) VALUES($1,$2,'market-hcap','亚洲让球','full_time','handicap',-0.25,$3,$4,'active',TRUE,$5,'provided')",uuid.uuid4(),fixture,selection,Decimal(price),now)
            await conn.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at,media_objects) VALUES($1,$2,$3,'1',$4,$5,$6)",MESSAGE,SOURCE,EXPERT,'测试主队甲 -0.25；测试主队乙 -0.25。两条买入。<script>window.__bad=1</script>',now,[{'object_key':'isolated/image-not-configured.png','mime_type':'image/png'}])
            await conn.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,'2','明晚比赛尚未明确方向，保留未解决原文',$4)",UNRESOLVED,SOURCE,EXPERT,now-timedelta(minutes=1))
            await conn.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key) VALUES($1,$2,'succeeded','web-integration-job')",JOB,MESSAGE)
            await conn.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,last_error) VALUES($1,$2,'unresolved','web-integration-unresolved','fixture_ambiguous')",uuid.uuid4(),UNRESOLVED)
            for index,(kind,payload) in enumerate([('model_input',{'text':'隔离原文文图输入','media':[{'type':'image_url'}]}),('tool_result',{'search_complete':True,'candidates':2}),('model_output',{'summary':'系统已接受两条推荐，第三个意图未匹配'})]):
                await conn.execute("INSERT INTO v2_analysis_traces(id,job_id,sequence_no,trace_kind,payload) VALUES($1,$2,$3,$4,$5)",uuid.uuid4(),JOB,index,kind,payload)
            for index in range(2):
                signal=uuid.uuid4()
                quote=await conn.fetchrow("SELECT * FROM v2_current_quotes WHERE fixture_id=$1 AND selection='home'",f'web-integration-match-{index}')
                await conn.execute("INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,period,market_kind,line,selection,recommendation_price,quote_time,confidence,score_baseline) VALUES($1,$2,$3,$4,$5,'market-hcap','亚洲让球','full_time','handicap',-0.25,'home',1.96,$6,0.9,$7)",signal,MESSAGE,EXPERT,quote['fixture_id'],'live' if index==0 else 'scheduled',now,{'home':1,'away':0})
                await conn.execute("INSERT INTO v2_quote_evidence(id,fixture_id,market_id,market_name,period,line,selection,price,evidence_kind,reference_id,source_updated_at,evidence) VALUES($1,$2,'market-hcap','亚洲让球','full_time',-0.25,'home',1.96,'expert_signal',$3,$4,$5)",uuid.uuid4(),quote['fixture_id'],signal,now,{'quote_id':str(quote['id'])})
            if os.environ.get('BROWSER_EXTENDED_FIXTURE') == 'true':
                # Public synthetic configuration, isolated API only; no provider network exists.
                for setting,value in {'auto_betting_enabled':'true', 'leyu_app_host':'https://synthetic.invalid',
                                      'leyu_app_signing_config':'{"synthetic":true}'}.items():
                    await conn.execute('INSERT INTO v2_admin_settings(setting_key,encrypted_value) VALUES($1,$2)',
                                       setting,encrypt_secret(bytes.fromhex(os.environ['DATA_ENCRYPTION_KEY']),value))
        print('isolated seed complete: 4 fixtures / 4 quotes / 2 messages / 2 signals / 3 traces')
    finally:
        await conn.close()
asyncio.run(seed())
