"""An unchanged requested subset must never suppress other native facts."""
import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock

import httpx

from services.collector.quote_snapshot import DeliveredFixtureSnapshots, fixture_envelope
from services.collector.leyu_protocol import LeyuError
from services.collector.worker import CollectorService
from test_collector_lifecycle import FakeRealtime, FakeRuntime, FakeTelegram
from test_unchanged_quote_refresh import sample


def fixture():
    return {'id': 'leyu:12345', 'source_match_id': '12345', 'phase': 'live',
            'clock_minute': 12, 'received_at': '2026-11-07T08:00:00+00:00',
            'source_updated_at': '2026-11-07T07:59:59+00:00', 'markets_complete': True,
            'markets': [{'market_id': '4', 'price': '1.95', 'received_at': 'old'},
                        {'market_id': '5', 'price': '2.05', 'received_at': 'old'}]}


class DeliveredFixtureSnapshotTests(unittest.TestCase):
    def test_no_prior_ack_is_not_a_delivery(self):
        self.assertFalse(DeliveredFixtureSnapshots().matches(fixture()))

    def test_only_receipt_clocks_are_ignored_and_input_is_untouched(self):
        cache = DeliveredFixtureSnapshots()
        old = fixture()
        original = deepcopy(old)
        cache.remember([old])
        current = deepcopy(old)
        current['received_at'] = 'new'
        current['markets'][0]['received_at'] = 'new'
        self.assertTrue(cache.matches(current))
        self.assertEqual(original, old)

    def test_every_other_native_fact_requires_delivery(self):
        cache = DeliveredFixtureSnapshots()
        old = fixture()
        cache.remember([old])
        for field, value in [('clock_minute', 13), ('phase', 'finished'),
                             ('source_updated_at', 'new'), ('markets_complete', False),
                             ('home_score', 1), ('home_team', 'renamed'), ('new_field', 1)]:
            with self.subTest(field=field):
                self.assertFalse(cache.matches({**old, field: value}))
        current = deepcopy(old)
        current['markets'][1]['price'] = '2.06'
        self.assertFalse(cache.matches(current))
        current['markets'][1]['price'] = '2.05'
        current['markets'][1]['source_updated_at'] = 'new'
        self.assertFalse(cache.matches(current))
        self.assertFalse(cache.matches({**old, 'markets': old['markets'][:1]}))

    def test_chunk_framing_and_normal_payload_represent_same_facts(self):
        old = fixture()
        old['markets'] = old['markets'] * 251
        cache = DeliveredFixtureSnapshots()
        cache.remember([fixture_envelope(old)])
        self.assertTrue(cache.matches(old))
        broken = fixture_envelope(old)
        broken['market_quote_count'] += 1
        self.assertFalse(cache.matches(broken))

    def test_invalid_or_unencodable_snapshot_never_confirms(self):
        cache = DeliveredFixtureSnapshots()
        for bad in ({'id': ''}, {**fixture(), 'markets': 'invalid'},
                    {**fixture(), 'new_field': object()}, {**fixture(), 'clock_minute': float('nan')}):
            with self.subTest(bad=str(type(bad))):
                cache.remember([bad])
                self.assertFalse(cache.matches(bad))

    def test_cache_is_bounded_and_eviction_requires_a_new_ack(self):
        cache = DeliveredFixtureSnapshots(capacity=2)
        old = fixture()
        cache.remember([old, {**old, 'id': 'two'}, {**old, 'id': 'three'}])
        self.assertEqual(2, len(cache._hashes))
        self.assertFalse(cache.matches(old))
        cache.remember([old])
        self.assertTrue(cache.matches(old))
        self.assertFalse(cache.matches({**old, 'id': 'two'}))
        cache.forget([old])
        self.assertFalse(cache.matches(old))


class DeliveredFixtureUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.mode = 'ok'

        async def handler(request):
            items = json.loads(request.content)['items']
            self.requests.append(items)
            for item in items:
                self.assertFalse(self.service._delivered_snapshots.matches(item))
            if self.mode == 'cancel':
                raise asyncio.CancelledError()
            if self.mode == 'transport':
                raise httpx.ReadTimeout('unknown commit', request=request)
            if self.mode == 'status':
                return httpx.Response(503)
            if self.mode == 'second_failure' and len(self.requests) == 2:
                return httpx.Response(503)
            if self.mode == 'invalid':
                return httpx.Response(200, json={'accepted': True})
            return httpx.Response(200, json={'accepted': 0 if self.mode == 'parent_old' else len(items)})

        self.api = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.service = CollectorService(self.api, 'http://api.invalid/internal', 'test-only',
                telegram_factory=FakeTelegram, runtime_factory=FakeRuntime, realtime_factory=FakeRealtime)

    async def asyncTearDown(self):
        await self.api.aclose()

    async def test_successful_parent_unchanged_receipt_is_a_committed_delivery(self):
        self.mode = 'parent_old'
        self.assertEqual(0, await self.service.upload([fixture()]))
        self.assertTrue(self.service._delivered_snapshots.matches(fixture()))

    async def test_unknown_failed_invalid_or_cancelled_upload_retires_prior_proof(self):
        for mode, error in (('transport', httpx.ReadTimeout), ('status', httpx.HTTPStatusError),
                            ('invalid', LeyuError), ('cancel', asyncio.CancelledError)):
            self.mode = 'ok'
            await self.service.upload([fixture()])
            self.mode = mode
            with self.subTest(mode=mode):
                with self.assertRaises(error):
                    await self.service.upload([fixture()])
                self.assertFalse(self.service._delivered_snapshots.matches(fixture()))

    async def test_partial_schedule_delivery_does_not_confirm_full_market_snapshot(self):
        await self.service.upload([fixture()], odds_mids=[])
        self.assertFalse(self.service._delivered_snapshots.matches(fixture()))

    async def test_chunked_success_remembers_all_unselected_quotes(self):
        current = fixture()
        current['markets'] *= 251
        await self.service.upload([current])
        self.assertIn('market_chunks', self.requests[0][0])
        self.assertTrue(self.service._delivered_snapshots.matches(current))

    async def test_later_batch_failure_does_not_invent_or_erase_other_batch_ack(self):
        first = fixture()
        first['markets'] *= 501
        second = {**deepcopy(first), 'id': 'leyu:other'}
        self.mode = 'second_failure'
        with self.assertRaises(httpx.HTTPStatusError):
            await self.service.upload([first, second])
        self.assertEqual(2, len(self.requests))
        self.assertTrue(self.service._delivered_snapshots.matches(first))
        self.assertFalse(self.service._delivered_snapshots.matches(second))

    async def test_precise_refresh_only_skips_after_full_ack_and_detects_unselected_change(self):
        for changed in (False, True):
            with self.subTest(unselected_changed=changed):
                native, observation, known = sample()
                native = json.loads(json.dumps(native, default=str))
                native['clock_minute'] = 12
                native['markets'].append({**native['markets'][0], 'market_id': 'other', 'price': '2.1'})
                observation = json.loads(json.dumps(observation, default=str))
                await self.service.upload([native])
                current = deepcopy(native)
                if changed:
                    current['markets'][1]['price'] = '2.2'
                before = len(self.requests)
                self.service.runtime.refresh_match = AsyncMock(return_value={**current, '_current_observation': observation})
                result = await self.service.leyu_refresh._refresh(native['id'], '12345', known)
                self.assertEqual(before + int(changed), len(self.requests))
                self.assertEqual('written' if changed else 'observed_unchanged', result['reason'])
