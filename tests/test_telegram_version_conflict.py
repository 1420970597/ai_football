"""Version conflict diagnostics and persistence use isolated PostgreSQL only."""
import unittest

import test_telegram_api as support
from app.routes.telegram import log_version_conflict


class ConflictLogTests(unittest.TestCase):
    def test_only_schema_field_names_are_logged(self):
        with self.assertLogs('platform.api', level='WARNING') as captured:
            log_version_conflict({'raw_text': '原文秘密', 'sender_id': '私密编号', 'extra-private': 1},
                                 {'raw_text': '其他秘密', 'sender_id': '另一编号'}, path='live')
        output = '\n'.join(captured.output)
        self.assertIn('fields=raw_text,sender_id', output)
        for value in ['原文秘密', '其他秘密', '私密编号', '另一编号', 'extra-private']:
            self.assertNotIn(value, output)


@unittest.skipUnless(support.TEST_URL, 'requires explicit temporary Telegram PostgreSQL')
class VersionConflictTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.TelegramApiTests.asyncSetUp
    asyncTearDown = support.TelegramApiTests.asyncTearDown
    event = support.TelegramApiTests.event
    post = support.TelegramApiTests.post

    async def test_live_conflict_reports_changed_fields_but_keeps_immutable_content(self):
        body = self.event()
        self.assertEqual(200, (await self.post(body)).status_code)
        with self.assertLogs('platform.api', level='WARNING') as captured:
            rejected = await self.post(body | {'raw_text': '不能覆盖的私密修改'})
        self.assertEqual(409, rejected.status_code)
        output = '\n'.join(captured.output)
        self.assertIn('path=live fields=raw_text', output)
        self.assertNotIn(body['raw_text'], output)
        self.assertNotIn('不能覆盖的私密修改', output)
        self.assertEqual(body['raw_text'], await self.pool.fetchval('SELECT raw_text FROM v2_expert_messages'))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_jobs'))

    async def test_identical_retry_has_one_event_and_no_conflict_log(self):
        body = self.event()
        self.assertEqual(200, (await self.post(body)).status_code)
        with self.assertNoLogs('platform.api', level='WARNING'):
            self.assertEqual(200, (await self.post(body)).status_code)
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_telegram_events'))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_jobs'))
