import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit, urlunsplit
import uuid

import asyncpg
from fastapi import FastAPI
import httpx

from app import admin_reviews as review_logic, auth, database, worker
from app.database import apply_migrations
from app.routes import admin_reviews
from app.security import token_hash

ADMIN_URL = os.environ.get('TEST_DATABASE_ADMIN_URL', '')
IMAGE = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aNuUAAAAASUVORK5CYII=')


@unittest.skipUnless(ADMIN_URL, '需要显式临时Docker数据库，不使用生产配置')
class AdminReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parts = urlsplit(ADMIN_URL)
        if (parts.hostname not in {'ai-football-v2-pgtest', 'ai-football-v2-pgverify'}
                or parts.username not in {'postgres', 'recovery_test'} or parts.path != '/postgres'):
            raise RuntimeError('管理员审核测试只接受具名临时Docker数据库')
        self.control = await asyncpg.connect(ADMIN_URL)
        self.name = 'test_admin_reviews_' + uuid.uuid4().hex
        await self.control.execute('CREATE DATABASE ' + self.name)
        url = urlunsplit((parts.scheme, parts.netloc, '/' + self.name, parts.query, ''))

        async def codecs(conn):
            await conn.set_type_codec('jsonb', schema='pg_catalog', encoder=json.dumps, decoder=json.loads)

        self.pool = await asyncpg.create_pool(url, min_size=1, max_size=6, init=codecs, command_timeout=5)
        async with self.pool.acquire() as conn:
            await apply_migrations(conn)
        database.pool = self.pool
        self.app = FastAPI()
        self.app.state.settings = SimpleNamespace(session_secret=b'isolated-admin-review-cursor-key!')
        self.app.include_router(admin_reviews.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test',
                                        headers={'Authorization': 'Bearer isolated-admin-one'})
        self.admin, self.admin_two, self.user = [uuid.uuid4() for _ in range(3)]
        for identity, role, token in ((self.admin, 'admin', 'isolated-admin-one'),
                                       (self.admin_two, 'admin', 'isolated-admin-two'),
                                       (self.user, 'user', 'isolated-user')):
            await self.pool.execute(
                "INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,$2,$3,'isolated-hash',$3)",
                identity, f'{identity}@test.invalid', role,
            )
            await self.pool.execute(
                "INSERT INTO v2_sessions(token_hash,csrf_hash,user_id,session_kind,expires_at,reauthenticated_at) "
                "VALUES($1,$2,$3,$4,now()+interval '1 hour',now())", token_hash(token), token_hash('isolated-csrf'), identity, role,
            )
        self.source, self.expert = uuid.uuid4(), uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'-100123456','群组名称')", self.source)
        await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'1001','TG专家昵称')", self.expert)
        await self.pool.execute('INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)', self.source, self.expert)
        self.message, self.job = await self.add_job()

    async def asyncTearDown(self):
        await self.client.aclose()
        database.pool = None
        await self.pool.close()
        await self.control.execute('DROP DATABASE ' + self.name + ' WITH (FORCE)')
        await self.control.close()

    async def add_job(self, state='unresolved', platform_id=None, revision=1, expert=None, source=None):
        message, job = uuid.uuid4(), uuid.uuid4()
        media = [{'object_key': 'telegram/private-image', 'mime_type': 'image/png', 'sha256': hashlib.sha256(IMAGE).hexdigest()}]
        await self.pool.execute(
            "INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at,revision,media_objects,reply_context) "
            "VALUES($1,$2,$3,$4,'原文：主队大2.5，图片补充',now(),$5,$6,$7)", message, source or self.source,
            expert or self.expert, platform_id or str(message), revision, media, {'raw_text': '被回复原文'},
        )
        await self.pool.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key) VALUES($1,$2,$3,$4)", job, message, state, str(job))
        await self.pool.execute(
            "INSERT INTO v2_analysis_traces(id,job_id,sequence_no,trace_kind,payload) VALUES($1,$2,1,'hermes_result',$3)",
            uuid.uuid4(), job, {'content': '未能唯一匹配，保留未解决', 'nested': {'api_key': 'do-not-display'},
                                'vision': 'data:image/png;base64,private-image-bytes'},
        )
        return message, job

    async def retry(self, job=None, key=None, reason='核实球队别名后重新审核'):
        return await self.client.post(f'/api/v1/admin/reviews/{job or self.job}/retry',
                                      json={'idempotency_key': str(key or uuid.uuid4()), 'reason': reason})

    async def test_admin_list_filters_time_and_keyset_pagination(self):
        await self.add_job('failed')
        await self.add_job('succeeded')
        base = {'expert_id': str(self.expert), 'source_id': str(self.source), 'limit': 1}
        seen, cursor = [], None
        while True:
            response = await self.client.get('/api/v1/admin/reviews', params=base | ({'cursor': cursor} if cursor else {}))
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual('private, no-store', response.headers['cache-control'])
            seen.extend(item['id'] for item in response.json()['items'])
            cursor = response.json()['next_cursor']
            if not cursor:
                break
        self.assertEqual(3, len(set(seen)))
        only_failed = await self.client.get('/api/v1/admin/reviews', params={'state': 'failed'})
        self.assertEqual(['failed'], [item['state'] for item in only_failed.json()['items']])
        missing_expert = await self.client.get('/api/v1/admin/reviews', params={'expert_id': str(uuid.uuid4())})
        self.assertEqual([], missing_expert.json()['items'])

    async def test_list_cursor_is_bound_to_actor_and_every_filter(self):
        await self.add_job()
        first = await self.client.get('/api/v1/admin/reviews', params={'limit': 1})
        cursor = first.json()['next_cursor']
        cases = [{'state': 'unresolved'}, {'expert_id': str(self.expert)}, {'source_id': str(self.source)},
                 {'from_utc': (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}, {'limit': 2}]
        for extra in cases:
            result = await self.client.get('/api/v1/admin/reviews', params={'limit': 1, 'cursor': cursor} | extra)
            self.assertEqual(400, result.status_code, result.text)
        tampered = await self.client.get('/api/v1/admin/reviews', params={'limit': 1, 'cursor': cursor[:-4] + 'AAAA'})
        self.assertEqual(400, tampered.status_code)
        actor = await self.client.get('/api/v1/admin/reviews', params={'limit': 1, 'cursor': cursor},
                                       headers={'Authorization': 'Bearer isolated-admin-two'})
        self.assertEqual(400, actor.status_code)

    async def test_invalid_or_unbounded_time_windows_rejected(self):
        for params in ({'from_utc': '2026-10-20T12:00:00'},
                       {'from_utc': '2026-10-21T00:00:00Z', 'to_utc': '2026-10-20T00:00:00Z'},
                       {'from_utc': '2024-01-01T00:00:00Z', 'to_utc': '2026-01-01T00:00:00Z'}):
            response = await self.client.get('/api/v1/admin/reviews', params=params)
            self.assertEqual(422, response.status_code, response.text)

    async def test_visitor_user_and_csrf_cannot_administer_reviews(self):
        for token, expected in (('', 401), ('isolated-user', 403)):
            response = await self.client.get('/api/v1/admin/reviews', headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(expected, response.status_code)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test') as cookie_client:
            cookie_client.cookies.set(auth.ADMIN_COOKIE, 'isolated-admin-one')
            cookie_client.cookies.set(auth.ADMIN_CSRF_COOKIE, 'isolated-csrf')
            response = await cookie_client.post(f'/api/v1/admin/reviews/{self.job}/retry',
                                                json={'reason': '核对后重试', 'idempotency_key': str(uuid.uuid4())})
            self.assertEqual(403, response.status_code)
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_jobs'))

    async def test_detail_contains_original_reply_nickname_and_authorized_media(self):
        result = await self.client.get(f'/api/v1/admin/reviews/{self.job}')
        self.assertEqual(200, result.status_code, result.text)
        body = result.json()
        self.assertIn('主队大2.5', body['message']['raw_text'])
        self.assertEqual('被回复原文', body['message']['reply_context']['raw_text'])
        self.assertEqual('TG专家昵称', body['message']['display_nickname'])
        self.assertTrue(body['retry_allowed'])
        self.assertNotIn('object_key', body['message']['media_objects'][0])
        media_url = body['message']['media_objects'][0]['url']
        with patch.object(admin_reviews, 'read_image', AsyncMock(return_value=IMAGE)):
            response = await self.client.get(media_url)
        self.assertEqual(IMAGE, response.content)
        self.assertEqual('private, no-store', response.headers['cache-control'])
        with patch.object(admin_reviews, 'read_image', AsyncMock(return_value=IMAGE + b'corruption')):
            corrupt = await self.client.get(media_url)
        self.assertEqual(503, corrupt.status_code)
        with patch.object(admin_reviews, 'read_image', AsyncMock(side_effect=RuntimeError('do-not-expose-provider-path'))):
            failed = await self.client.get(media_url)
        self.assertEqual(503, failed.status_code)
        self.assertNotIn('do-not-expose', failed.text)

    async def test_traces_paginate_beyond_twenty_and_redact_credentials(self):
        await self.pool.execute('DELETE FROM v2_analysis_traces WHERE job_id=$1', self.job)
        await self.pool.executemany(
            'INSERT INTO v2_analysis_traces(id,job_id,sequence_no,trace_kind,payload) VALUES($1,$2,$3,$4,$5)',
            [(uuid.uuid4(), self.job, index, 'tool_result', {'content': f'工具结果{index}', 'nested': {'api_key': 'do-not-display'},
                                                          'image': 'data:image/png;base64,private-bytes'}) for index in range(1, 46)],
        )
        cursor, seen = None, []
        while True:
            response = await self.client.get(f'/api/v1/admin/reviews/{self.job}/traces',
                                              params={'limit': 20} | ({'cursor': cursor} if cursor else {}))
            self.assertEqual(200, response.status_code, response.text)
            self.assertNotIn('do-not-display', response.text)
            self.assertNotIn('private-bytes', response.text)
            seen.extend(item['sequence_no'] for item in response.json()['items'])
            cursor = response.json()['next_cursor']
            if not cursor:
                break
        self.assertEqual(list(range(1, 46)), seen)
        first = (await self.client.get(f'/api/v1/admin/reviews/{self.job}/traces', params={'limit': 20})).json()
        _, other = await self.add_job()
        cross = await self.client.get(f'/api/v1/admin/reviews/{other}/traces', params={'limit': 20, 'cursor': first['next_cursor']})
        self.assertEqual(400, cross.status_code)

    async def test_retry_creates_new_job_preserves_old_history_and_worker_can_claim(self):
        response = await self.retry()
        self.assertEqual(201, response.status_code, response.text)
        new_job = uuid.UUID(response.json()['id'])
        self.assertNotEqual(self.job, new_job)
        self.assertEqual('unresolved', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))
        self.assertEqual('hermes_result', await self.pool.fetchval('SELECT trace_kind FROM v2_analysis_traces WHERE job_id=$1 AND sequence_no=1', self.job))
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='review_retry_requested'"))
        detail = (await self.client.get(f'/api/v1/admin/reviews/{new_job}')).json()
        self.assertEqual(str(self.job), detail['retry_of']['original_job_id'])
        claimed = await worker.claim_job(self.pool)
        self.assertEqual(new_job, claimed['id'])
        self.assertEqual('TG专家昵称', claimed['message']['display_nickname'])
        self.assertEqual('unresolved', await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1', self.job))

    async def test_identical_retry_is_idempotent_and_changed_body_conflicts(self):
        key = uuid.uuid4()
        first = await self.retry(key=key)
        replay = await self.retry(key=key)
        self.assertEqual(first.json()['id'], replay.json()['id'])
        self.assertTrue(replay.json()['replayed'])
        self.assertEqual(409, (await self.retry(key=key, reason='另一个不同的重试原因')).status_code)
        _, other = await self.add_job()
        self.assertEqual(409, (await self.retry(job=other, key=key)).status_code)
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_admin_review_retries'))

    async def test_concurrent_different_retry_keys_enqueue_only_once(self):
        first, second = await asyncio.gather(self.retry(), self.retry())
        self.assertEqual([201, 409], sorted([first.status_code, second.status_code]))
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_analysis_jobs WHERE state IN ('queued','running','retry')"))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_admin_review_retries'))

    async def test_concurrent_same_retry_key_replays_same_job(self):
        key = uuid.uuid4()
        responses = await asyncio.gather(self.retry(key=key), self.retry(key=key))
        self.assertEqual([201, 201], [item.status_code for item in responses])
        self.assertEqual(responses[0].json()['id'], responses[1].json()['id'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_admin_review_retries'))

    async def test_only_terminal_failed_unresolved_can_retry(self):
        for state in ('queued', 'running', 'retry', 'succeeded'):
            await self.pool.execute('UPDATE v2_analysis_jobs SET state=$2 WHERE id=$1', self.job, state)
            response = await self.retry()
            self.assertEqual(409, response.status_code, response.text)
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='failed' WHERE id=$1", self.job)
        self.assertEqual(201, (await self.retry()).status_code)

    async def test_newer_revision_deleted_untrusted_and_incomplete_media_rejected(self):
        changes = [("UPDATE v2_expert_sources SET enabled=false", "UPDATE v2_expert_sources SET enabled=true"),
                   ("UPDATE v2_experts SET enabled=false", "UPDATE v2_experts SET enabled=true"),
                   ("UPDATE v2_source_experts SET enabled=false", "UPDATE v2_source_experts SET enabled=true"),
                   ("UPDATE v2_expert_sources SET allow_media=false", "UPDATE v2_expert_sources SET allow_media=true"),
                   ("UPDATE v2_expert_messages SET media_state='pending'", "UPDATE v2_expert_messages SET media_state='ready'"),
                   ("UPDATE v2_expert_messages SET deleted_at=now()", "UPDATE v2_expert_messages SET deleted_at=NULL")]
        for change, reset in changes:
            await self.pool.execute(change)
            response = await self.retry()
            self.assertEqual(409, response.status_code, response.text)
            await self.pool.execute(reset)
        platform = await self.pool.fetchval('SELECT platform_message_id FROM v2_expert_messages WHERE id=$1', self.message)
        await self.add_job(platform_id=platform, revision=2)
        response = await self.retry()
        self.assertEqual('message_deleted_or_superseded', response.json()['detail']['code'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_admin_review_retries'))

    async def test_old_current_event_or_another_active_job_prevents_retry(self):
        await self.pool.execute(
            "INSERT INTO v2_telegram_events(id,source_id,platform_message_id,version_key,source_version_at,event_kind,content,media_state,message_id,is_current) "
            "SELECT $1,source_id,platform_message_id,'synthetic-version',now(),'message','{}'::jsonb,'ready',id,false FROM v2_expert_messages WHERE id=$2",
            uuid.uuid4(), self.message,
        )
        self.assertEqual(409, (await self.retry()).status_code)
        await self.pool.execute('UPDATE v2_telegram_events SET is_current=true')
        await self.pool.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key) VALUES($1,$2,'queued',$3)", uuid.uuid4(), self.message, str(uuid.uuid4()))
        response = await self.retry()
        self.assertEqual('another_review_is_active', response.json()['detail']['code'])
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.pool.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key) VALUES($1,$2,'queued',$3)", uuid.uuid4(), self.message, str(uuid.uuid4()))

    async def test_accepted_withdrawn_signal_in_any_revision_is_never_replayed(self):
        await self.pool.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) VALUES('synthetic','synthetic','football','L','A','B',now(),'scheduled')")
        await self.pool.execute(
            "INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,line,selection,recommendation_price,quote_time,confidence,withdrawn_at) "
            "VALUES($1,$2,$3,'synthetic','scheduled','1','独赢',0,'home',1.95,now(),.9,now())", uuid.uuid4(), self.message, self.expert,
        )
        platform = await self.pool.fetchval('SELECT platform_message_id FROM v2_expert_messages WHERE id=$1', self.message)
        _, latest_job = await self.add_job(platform_id=platform, revision=2)
        for job in (self.job, latest_job):
            response = await self.retry(job=job)
            self.assertEqual(409, response.status_code)
            self.assertEqual('accepted_signals_are_immutable', response.json()['detail']['code'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_missing_reason_or_recent_auth_cannot_enqueue(self):
        self.assertEqual(422, (await self.retry(reason='   ')).status_code)
        await self.pool.execute("UPDATE v2_sessions SET reauthenticated_at=now()-interval '1 hour'")
        self.assertEqual(403, (await self.retry()).status_code)
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_jobs'))

    async def test_audit_failure_rolls_back_job_trace_and_retry_ledger(self):
        with patch.object(review_logic, 'audit', AsyncMock(side_effect=RuntimeError('isolated-write-failure'))):
            with self.assertRaises(RuntimeError):
                await self.retry()
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_jobs'))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_traces'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_admin_review_retries'))

    async def test_telegram_edit_serializes_before_retry_checks_latest_revision(self):
        platform = await self.pool.fetchval('SELECT platform_message_id FROM v2_expert_messages WHERE id=$1', self.message)
        task = None
        async with self.pool.acquire() as editor:
            async with editor.transaction():
                await editor.execute('SELECT pg_advisory_xact_lock(hashtext($1))', f'telegram:{self.source}:{platform}')
                task = asyncio.create_task(self.retry())
                await asyncio.sleep(0.03)
                self.assertFalse(task.done())
                await editor.execute(
                    "INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at,revision) "
                    "VALUES($1,$2,$3,$4,'编辑后的最新原文',now(),2)", uuid.uuid4(), self.source, self.expert, platform,
                )
        response = await task
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual('message_deleted_or_superseded', response.json()['detail']['code'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_jobs'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_admin_review_retries'))


if __name__ == '__main__':
    unittest.main()
