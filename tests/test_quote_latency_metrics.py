"""Quote clocks remain distinct from enclosing event clocks in telemetry."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch

from services.collector.leyu_realtime import LATENCY_WINDOW_SIZE, LeyuRealtime


class QuoteLatencyMetricsTests(unittest.IsolatedAsyncioTestCase):
    def service(self, on_batch=None):
        return LeyuRealtime(lambda: {}, lambda: [], on_batch or AsyncMock(), AsyncMock())

    def event(self, ages=(2,), *, now=None, outer_age=1):
        now = now or datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
        quotes = [{'market_id': str(index), 'source_updated_at': (now-timedelta(seconds=age)).isoformat(),
                   'received_at': now.isoformat()} for index, age in enumerate(ages)]
        return {'fixture_id': 'leyu:isolated', 'command': 'C105', 'payload': {'version': 1},
                'source_updated_at': (now-timedelta(seconds=outer_age)).isoformat(),
                'received_at': now.isoformat(), 'markets': quotes}

    async def test_actual_inner_quote_clocks_not_outer_event_clock(self):
        service = self.service()
        event = self.event((20, 40), outer_age=1)
        original = deepcopy(event)
        service._record_event(event)
        self.assertEqual(1000, service.stats['source_lag_ms_p50'])
        self.assertEqual(20000, service.stats['quote_source_lag_ms_p50'])
        self.assertEqual(2, service.stats['quote_source_lag_samples'])
        self.assertEqual(2, service.stats['quote_source_lag_known_count'])
        self.assertEqual(original, event)

    async def test_unknown_invalid_future_and_thirty_day_old_remain_distinct(self):
        service = self.service()
        event = self.event((2,))
        now = datetime.fromisoformat(event['received_at'])
        for value in (None, '', 'invalid', '2026-10-27T11:59:00',
                      (now+timedelta(seconds=1)).isoformat(),
                      (now-timedelta(days=30)).isoformat()):
            event['markets'].append({'source_updated_at': value})
        service._record_event(event)
        for key, value in {'window_size': 7, 'samples': 1, 'known_count': 3, 'unknown_count': 2,
                           'invalid_count': 2, 'future_count': 1, 'too_old_count': 1}.items():
            self.assertEqual(value, service.stats['quote_source_lag_' + key], key)
        self.assertEqual(2000, service.stats['quote_source_lag_ms_p95'])

    async def test_receiving_observation_prefers_quote_receipt_and_preserves_unknown_source(self):
        service = self.service()
        event = self.event((2,))
        received = datetime.fromisoformat(event['received_at'])
        event['markets'][0]['received_at'] = (received+timedelta(seconds=3)).isoformat()
        event['markets'].append({'source_updated_at': None})
        service._record_event(event)
        self.assertEqual(5000, service.stats['quote_source_lag_ms_p50'])
        self.assertEqual(1, service.stats['quote_source_lag_unknown_count'])
        self.assertIsNone(event['markets'][1]['source_updated_at'])

    async def test_rolling_256_window_evicts_old_unknown_and_known_samples(self):
        service = self.service()
        event = self.event(())
        now = datetime.fromisoformat(event['received_at'])
        event['markets'] = [{'source_updated_at': None}] * 30 + [
            {'source_updated_at': (now-timedelta(seconds=age)).isoformat()} for age in range(1, 301)]
        service._record_event(event)
        values = service.stats
        self.assertEqual(LATENCY_WINDOW_SIZE, len(service._quote_source_lags))
        self.assertEqual(256, values['quote_source_lag_window_size'])
        self.assertEqual(256, values['quote_source_lag_known_count'])
        self.assertEqual(0, values['quote_source_lag_unknown_count'])
        self.assertEqual(172000, values['quote_source_lag_ms_p50'])
        self.assertEqual(287000, values['quote_source_lag_ms_p95'])
        self.assertEqual(256, values['quote_source_lag_samples'])

    async def test_unknown_only_window_clears_previous_percentiles(self):
        service = self.service()
        service._record_event(self.event())
        event = self.event(())
        event['markets'] = [{'source_updated_at': None}] * 256
        service._record_event(event)
        self.assertIsNone(service.stats['quote_source_lag_ms_p50'])
        self.assertIsNone(service.stats['quote_source_lag_ms_p95'])
        self.assertEqual(0, service.stats['quote_source_lag_samples'])
        self.assertEqual(256, service.stats['quote_source_lag_unknown_count'])

    async def test_multiple_quotes_successfully_delivered_use_one_completion_instant(self):
        received = datetime.now(timezone.utc)-timedelta(seconds=1)
        event = self.event((20, 40), now=received)
        stop = asyncio.Event()
        calls = []
        async def delivered(batch):
            calls.append(deepcopy(batch))
            stop.set()
        service = self.service(delivered)
        service._enqueue_event(event)
        await service._consume(stop)
        self.assertEqual(1, len(calls))
        self.assertEqual(2, service.stats['quote_delivery_age_samples'])
        self.assertEqual(2, service.stats['quote_delivery_age_known_count'])
        self.assertGreaterEqual(service.stats['quote_delivery_age_ms_p50'], 21000)
        self.assertLess(service.stats['quote_delivery_age_ms_p50'], 30000)
        self.assertEqual(1, service.stats['delivery_age_samples'])
        self.assertEqual(2, service.stats['quote_source_lag_samples'])

    async def test_failed_delivery_has_no_false_success_metrics_and_retry_counts_once(self):
        event = self.event((2,), now=datetime.now(timezone.utc))
        stop = asyncio.Event()
        calls = []
        service = self.service()
        async def delivered(batch):
            calls.append(deepcopy(batch))
            self.assertEqual(0, service.stats['quote_delivery_age_samples'])
            if len(calls) == 1:
                raise RuntimeError('isolated_delivery_failure')
            stop.set()
        service.on_batch = delivered
        service._enqueue_event(event)
        with patch('services.collector.leyu_realtime.log.warning'):
            await service._consume(stop)
        self.assertEqual(2, len(calls))
        self.assertEqual(1, service.stats['quote_delivery_age_samples'])
        self.assertEqual(1, service.stats['delivery_failures'])

    async def test_identical_frames_count_observed_quotes_but_only_one_delivered_frame(self):
        stop = asyncio.Event()
        async def delivered(batch):
            self.assertEqual(1, len(batch))
            stop.set()
        service = self.service(delivered)
        event = self.event((2, 3), now=datetime.now(timezone.utc))
        service._enqueue_event(event)
        service._enqueue_event(deepcopy(event))
        await service._consume(stop)
        self.assertEqual(4, service.stats['quote_source_lag_window_size'])
        self.assertEqual(2, service.stats['quote_delivery_age_window_size'])
        self.assertEqual(1, service.stats['events_delivered'])

    async def test_delivery_window_is_also_bounded_and_too_old_is_not_unknown(self):
        service = self.service()
        now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
        event = self.event(tuple(range(1, 301)), now=now)
        service._record_quote_latency(event, service._quote_delivery_ages, 'quote_delivery_age', now)
        self.assertEqual(256, len(service._quote_delivery_ages))
        self.assertEqual(256, service.stats['quote_delivery_age_window_size'])
        self.assertEqual(172000, service.stats['quote_delivery_age_ms_p50'])
        old = self.event((), now=now)
        old['markets'] = [{'source_updated_at': (now-timedelta(days=30)).isoformat()}] * 256
        service._record_quote_latency(old, service._quote_delivery_ages, 'quote_delivery_age', now)
        self.assertEqual(256, service.stats['quote_delivery_age_known_count'])
        self.assertEqual(256, service.stats['quote_delivery_age_too_old_count'])
        self.assertEqual(0, service.stats['quote_delivery_age_unknown_count'])
        self.assertEqual(0, service.stats['quote_delivery_age_samples'])
        self.assertIsNone(service.stats['quote_delivery_age_ms_p95'])


if __name__ == '__main__':
    unittest.main()
