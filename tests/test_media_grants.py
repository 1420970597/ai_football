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
