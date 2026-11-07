"""A completed native read is distinct from an unconfirmed projection write."""
import asyncio
from copy import deepcopy
from decimal import Decimal
import unittest

import test_unchanged_quote_refresh as support


class ProjectionTimeoutObservationTests(unittest.IsolatedAsyncioTestCase):
    def controller(self):
        service, runtime, upload, fixture, known = support.UnchangedQuoteRefreshTests().controller()
        service.can_skip_upload = lambda current: False
        service.deadline_seconds = .03
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def pending(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        upload.side_effect = pending
        return service, runtime, upload, fixture, known, entered, cancelled

    async def test_completed_exact_observation_survives_upload_deadline_without_write_claim(self):
        service, runtime, upload, fixture, known, _, cancelled = self.controller()
        result = await service.refresh({'fixture_id': fixture['id'], 'source_match_id': '12345',
                                        'known_snapshot': known})
        self.assertEqual('observed_current_projection_unconfirmed', result['reason'])
        self.assertFalse(result['accepted'])
        self.assertFalse(result['written'])
        self.assertFalse(result['snapshot_committed'])
        self.assertTrue(result['current_only'])
        self.assertEqual(0, result['written_fixtures'])
        self.assertEqual(fixture['markets'][0]['source_updated_at'],
                         result['current_observation']['quotes'][0]['source_updated_at'])
        self.assertTrue(cancelled.is_set())
        self.assertFalse(service._inflight)
        runtime.refresh_match.assert_awaited_once_with('12345')
        upload.assert_awaited_once()

    async def test_unknown_snapshot_does_not_make_projection_timeout_current(self):
        service, _, upload, fixture, _, _, cancelled = self.controller()
        result = await service.refresh({'fixture_id': fixture['id'], 'source_match_id': '12345'})
        self.assertEqual('refresh_timeout', result['reason'])
        self.assertNotIn('current_observation', result)
        self.assertTrue(cancelled.is_set())
        upload.assert_awaited_once()

    async def test_changed_or_malformed_native_evidence_does_not_survive_timeout(self):
        for change in ('price', 'score', 'source', 'path', 'duplicate', 'missing'):
            with self.subTest(change=change):
                service, runtime, upload, fixture, known, _, cancelled = self.controller()
                returned = runtime.refresh_match.return_value
                observation = deepcopy(returned['_current_observation'])
                if change == 'price': observation['quotes'][0]['price'] = Decimal('2.03')
                elif change == 'score': observation['context']['home_score'] = 1
                elif change == 'source': observation['source_match_id'] = 'other'
                elif change == 'path': observation['path'] = '/not-an-authorized-detail'
                elif change == 'duplicate': observation['quotes'].append(deepcopy(observation['quotes'][0]))
                else: observation['quotes'] = []
                returned['_current_observation'] = observation
                returned['markets'] = deepcopy(observation['quotes'])
                result = await service.refresh({'fixture_id': fixture['id'], 'source_match_id': '12345',
                                                'known_snapshot': known})
                self.assertEqual('refresh_timeout', result['reason'])
                self.assertNotIn('current_observation', result)
                self.assertTrue(cancelled.is_set())
                upload.assert_awaited_once()

    async def test_source_deadline_does_not_return_an_observation_or_upload(self):
        service, runtime, upload, fixture, known, _, _ = self.controller()
        async def source(_):
            await asyncio.Event().wait()
        runtime.refresh_match.side_effect = source
        result = await service.refresh({'fixture_id': fixture['id'], 'source_match_id': '12345',
                                        'known_snapshot': known})
        self.assertEqual('refresh_timeout', result['reason'])
        self.assertNotIn('current_observation', result)
        upload.assert_not_awaited()

    async def test_caller_cancellation_cannot_become_a_timeout_witness(self):
        service, runtime, upload, fixture, known, entered, cancelled = self.controller()
        service.deadline_seconds = 1
        task = asyncio.create_task(service.refresh({'fixture_id': fixture['id'], 'source_match_id': '12345',
                                                    'known_snapshot': known}))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertFalse(service._inflight)
        runtime.refresh_match.assert_awaited_once_with('12345')
        upload.assert_awaited_once()
