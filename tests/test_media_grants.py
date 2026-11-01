"""Offline media-ticket boundary tests plus real isolated database revocation."""
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import media_grants
import test_message_evidence_reading as reading
import test_admin_reviews as support


class MediaGrantTests(unittest.TestCase):
    def setUp(self):
        self.key = b'isolated-media-signing-key-32bytes'
        self.actor = {'id': 'user', '_token_hash': 'session'}
        self.message = {'id': 'message', 'revision': 2}
        self.media = {'object_key': 'telegram/messages/private', 'sha256': 'a'*64}
        self.grant = media_grants.issue(self.key, self.actor, self.message, 0, self.media, 'user', now=1000)

    def verify(self, **changes):
        values = {'key': self.key, 'actor': self.actor, 'message': self.message, 'index': 0,
                  'media': self.media, 'context': 'user', 'grant': self.grant, 'now': 1001}
        values.update(changes)
        return media_grants.verify(**values)

    def test_valid_and_near_expiry_ticket(self):
        self.verify()
        self.verify(now=1059.999)
        self.assertNotIn('telegram/messages/private', self.grant)

    def test_missing_tampered_expired_future_or_wrong_key_denied(self):
        for changes in ({'grant': None}, {'grant': 'invalid'}, {'grant': self.grant[:-8]+'AAAAAAAA'},
                        {'now': 1060}, {'now': 999}, {'key': b'x'*32}):
            with self.subTest(changes=changes), self.assertRaises(HTTPException) as error:
                self.verify(**changes)
            self.assertEqual(403, error.exception.status_code)

    def test_identity_context_version_object_and_index_are_bound(self):
        for changes in ({'actor': {**self.actor, 'id': 'other'}},
                        {'actor': {**self.actor, '_token_hash': 'other-session'}},
                        {'message': {**self.message, 'revision': 3}},
                        {'message': {**self.message, 'id': 'other-message'}},
                        {'media': {**self.media, 'object_key': 'other'}},
                        {'media': {**self.media, 'sha256': 'b'*64}},
                        {'context': 'admin'}, {'index': 1}):
            with self.subTest(changes=changes), self.assertRaises(HTTPException):
                self.verify(**changes)


@unittest.skipUnless(support.ADMIN_URL, 'requires explicit isolated PostgreSQL')
class MediaGrantDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.AdminReviewTests.asyncSetUp
    asyncTearDown = support.AdminReviewTests.asyncTearDown
    add_job = support.AdminReviewTests.add_job
    prepare = reading.MessageEvidenceReadingTests.prepare
    path = reading.MessageEvidenceReadingTests.path
    async def test_admin_list_preview_issues_private_same_session_media_grant(self):
        await self.prepare()
        self.client.headers['Authorization']='Bearer isolated-admin-one'
        self.client.headers['X-Session-Context']='admin'
        original=await self.pool.fetchval('SELECT media_objects FROM v2_expert_messages WHERE id=$1',self.message)
        response=await self.client.get('/api/v1/admin/telegram/messages',params={'limit':1})
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual('private, no-store',response.headers['cache-control'])
        message=response.json()['items'][0]
        self.assertEqual(str(self.message),message['id'])
        self.assertEqual(str(self.expert),message['expert_id'])
        self.assertEqual(str(self.source),message['source_id'])
        media=message['media_objects'][0]
        self.assertNotIn('object_key',media)
        self.assertNotIn('telegram/private-image',response.text)
        self.assertEqual(60,media['expires_in_seconds'])
        url=media['url']
        self.assertTrue(url.startswith('/api/v1/admin/telegram/messages/'+str(self.message)+'/media/0?grant='))
        with patch('app.routes.expert_content.read_image',return_value=support.IMAGE) as read:
            image=await self.client.get(url)
            self.assertEqual(200,image.status_code,image.text if image.status_code!=200 else '')
            self.assertEqual(support.IMAGE,image.content)
            self.assertEqual('private, no-store',image.headers['cache-control'])
            self.assertEqual(403,(await self.client.get(url.split('?')[0])).status_code)
            self.client.headers['Authorization']='Bearer isolated-admin-two'
            self.assertEqual(403,(await self.client.get(url)).status_code)
            self.client.headers['Authorization']='Bearer isolated-user'
            self.client.headers['X-Session-Context']='user'
            self.assertEqual(403,(await self.client.get('/api/v1/admin/telegram/messages')).status_code)
            self.assertEqual(403,(await self.client.get(url)).status_code)
            self.assertEqual(1,read.call_count)
        self.assertEqual(original,await self.pool.fetchval('SELECT media_objects FROM v2_expert_messages WHERE id=$1',self.message))

    async def test_admin_preview_grant_expires_and_message_edit_requires_new_list(self):
        import time
        await self.prepare()
        self.client.headers['Authorization']='Bearer isolated-admin-one'
        self.client.headers['X-Session-Context']='admin'
        async def preview_url():
            response=await self.client.get('/api/v1/admin/telegram/messages')
            self.assertEqual(200,response.status_code,response.text)
            return response.json()['items'][0]['media_objects'][0]['url']
        old=await preview_url()
        with patch('app.routes.expert_content.read_image',return_value=support.IMAGE) as read:
            with patch('app.media_grants.time.time',return_value=time.time()+61):
                self.assertEqual(403,(await self.client.get(old)).status_code)
            await self.pool.execute('UPDATE v2_expert_messages SET revision=revision+1 WHERE id=$1',self.message)
            self.assertEqual(403,(await self.client.get(old)).status_code)
            read.assert_not_called()
            self.assertEqual(200,(await self.client.get(await preview_url())).status_code)
            self.assertEqual(1,read.call_count)

    async def test_signed_image_expiry_revoke_edit_and_integrity(self):
        await self.prepare()
        path = await self.path()
        detail = await self.client.get(path)
        url = detail.json()['message']['media_objects'][0]['url']
        self.assertIn('?grant=', url)
        with patch('app.routes.expert_content.read_image', return_value=support.IMAGE) as read:
            self.assertEqual(200, (await self.client.get(url)).status_code)
            self.assertEqual(403, (await self.client.get(path+'/media/0')).status_code)
            with patch('app.media_grants.time.time', return_value=__import__('time').time()+61):
                self.assertEqual(403, (await self.client.get(url)).status_code)
            self.assertEqual(1, read.call_count)
        await self.pool.execute("UPDATE v2_memberships SET ends_at=now()-interval '1 second' WHERE user_id=$1", self.user)
        with patch('app.routes.expert_content.read_image') as read:
            self.assertEqual(403, (await self.client.get(url)).status_code)
            read.assert_not_called()
        await self.pool.execute("UPDATE v2_memberships SET ends_at=now()+interval '1 day' WHERE user_id=$1", self.user)
        await self.pool.execute('UPDATE v2_expert_messages SET revision=revision+1 WHERE id=$1', self.message)
        self.assertEqual(403, (await self.client.get(url)).status_code)
        url = (await self.client.get(path)).json()['message']['media_objects'][0]['url']
        with patch('app.routes.expert_content.read_image', return_value=support.IMAGE+b'corrupt'):
            response = await self.client.get(url)
        self.assertEqual(503, response.status_code)
        self.assertEqual('private, no-store', response.headers['cache-control'])
