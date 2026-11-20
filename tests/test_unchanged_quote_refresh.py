"""Exact observations may avoid identical projection writes, never validation."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from services.collector.leyu_refresh import LeyuRefreshController, LeyuRequestBudget


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def sample():
    now = datetime.now(timezone.utc)
    quote = {'market_id': '4', 'market_kind': 'handicap', 'period': 'full_time',
        'line': Decimal('-0.50'), 'selection': 'home', 'price': Decimal('1.95000'),
        'source_updated_at': now-timedelta(hours=1), 'status': 'active', 'play_id': '4',
        'market_instance_id': 'native-market', 'selection_id': 'native-home', 'score_basis': 'full_period'}
    context = {'phase': 'live', 'home_score': 0, 'away_score': 0,
               'half_home_score': 0, 'half_away_score': 0}
    fixture = {'id': 'leyu:12345', 'source_id': 'leyu:12345', 'source_match_id': '12345',
               **context, 'markets': [quote], 'markets_complete': True}
    observation = {'path': '/yewu11/v1/w/getMatchBaseInfoByOddsPB', 'fixture_id': fixture['id'],
        'source_match_id': '12345', 'started_at': (now-timedelta(seconds=2)).isoformat(),
        'completed_at': (now-timedelta(seconds=1)).isoformat(), 'context': context, 'quotes': [quote]}
    # Independently specified normalized wire contract, rather than calling the implementation.
    wire = {**quote, 'line': '-0.5', 'price': '1.95', 'source_updated_at': quote['source_updated_at'].isoformat()}
    known = {'context_sha256': digest(context), 'quote_sha256': [digest(wire)]}
    return fixture, observation, known


class UnchangedQuoteRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def started(self, task, entered):
        waiter = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait((task, waiter), timeout=1,
                                         return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task  # Surface an immediate protocol error, not an infinite wait.
            if waiter not in done:
                raise TimeoutError('isolated source read did not start')
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    def controller(self, observation=None, fetch=None):
        fixture, default, known = sample()
        observation = default if observation is None else observation
        returned = {**fixture, 'markets': deepcopy(observation.get('quotes', [])),
                    '_current_observation': deepcopy(observation)}
        runtime = SimpleNamespace(fixtures={fixture['id']: fixture},
            request_budget=LeyuRequestBudget(), refresh_match=fetch or AsyncMock(return_value=returned))
        upload = AsyncMock(return_value=1)
        return LeyuRefreshController(runtime, upload, can_skip_upload=lambda current: True), runtime, upload, fixture, known

    async def test_without_full_delivery_proof_keeps_original_upload(self):
        controller, _, upload, fixture, known = self.controller()
        controller.can_skip_upload = None
        result = await controller.refresh({'fixture_id': fixture['id'], 'source_match_id': '12345',
                                           'known_snapshot': known})
        upload.assert_awaited_once()
        self.assertEqual('written', result['reason'])

    async def test_other_native_facts_changed_keeps_original_upload(self):
        controller, _, upload, fixture, known = self.controller()
        controller.can_skip_upload = lambda current: False
        result = await controller.refresh({'fixture_id': fixture['id'], 'source_match_id': '12345',
                                           'known_snapshot': known})
        upload.assert_awaited_once()
        self.assertEqual('written', result['reason'])

    async def test_failed_delivery_comparison_keeps_original_upload(self):
        controller, _, upload, fixture, known = self.controller()
        def invalid(current):
            raise ValueError('invalid delivery proof')
        controller.can_skip_upload = invalid
        result = await controller.refresh({'fixture_id': fixture['id'], 'source_match_id': '12345',
                                           'known_snapshot': known})
        upload.assert_awaited_once()
        self.assertEqual('written', result['reason'])

    async def test_exact_known_quotes_are_observed_without_claiming_a_write(self):
        controller, runtime, upload, fixture, known = self.controller()
        result = await controller.refresh({'fixture_id': fixture['id'], 'source_match_id': '12345',
                                           'known_snapshot': known})
        self.assertEqual('observed_unchanged', result['reason'])
        self.assertFalse(result['accepted'])
        self.assertFalse(result['written'])
        self.assertEqual(0, result['written_fixtures'])
        self.assertFalse(result['snapshot_committed'])
        self.assertTrue(result['current_only'])
        upload.assert_not_awaited()
        runtime.refresh_match.assert_awaited_once_with('12345')
        self.assertEqual(fixture['markets'][0]['source_updated_at'],
                         result['current_observation']['quotes'][0]['source_updated_at'])
        self.assertEqual({}, controller._inflight)

    async def test_each_quote_identity_or_context_change_keeps_the_original_upload(self):
        changes = {'market_id': '5', 'market_kind': 'total', 'period': 'first_half',
            'line': Decimal('-0.75'), 'selection': 'away', 'price': Decimal('1.96'),
            'source_updated_at': datetime.now(timezone.utc), 'status': 'closed', 'play_id': '5',
            'market_instance_id': 'other', 'selection_id': 'other', 'score_basis': 'remaining'}
        for field, value in changes.items():
            with self.subTest(quote_field=field):
                fixture, observation, known = sample()
                observation['quotes'][0][field] = value
                controller, _, upload, _, _ = self.controller(observation)
                result = await controller.refresh({'fixture_id': fixture['id'], 'source_match_id': '12345',
                                                   'known_snapshot': known})
                upload.assert_awaited_once()
                self.assertEqual('written', result['reason'])
                self.assertTrue(result['snapshot_committed'])
        for field, value in {'phase': 'finished', 'home_score': 1, 'away_score': 1,
                             'half_home_score': 1, 'half_away_score': 1}.items():
            with self.subTest(context_field=field):
                fixture, observation, known = sample()
                observation['context'][field] = value
                controller, _, upload, _, _ = self.controller(observation)
                result = await controller.refresh({'fixture_id': fixture['id'], 'source_match_id': '12345',
                                                   'known_snapshot': known})
                upload.assert_awaited_once()
                self.assertEqual('written', result['reason'])

    async def test_missing_duplicate_or_wrong_source_observation_does_not_skip_upload(self):
        for change in ({'quotes': []}, {'path': '/not-detail'}, {'fixture_id': 'leyu:999'},
                       {'source_match_id': '999'}, {'context': {}}):
            with self.subTest(change=change):
                fixture, observation, known = sample()
                observation.update(change)
                controller, _, upload, _, _ = self.controller(observation)
                await controller.refresh({'fixture_id': fixture['id'], 'source_match_id': '12345',
                                           'known_snapshot': known})
                upload.assert_awaited_once()
        fixture, observation, known = sample()
        observation['quotes'].append(deepcopy(observation['quotes'][0]))
        controller, _, upload, _, _ = self.controller(observation)
        await controller.refresh({'fixture_id': fixture['id'], 'source_match_id': '12345', 'known_snapshot': known})
        upload.assert_awaited_once()

    async def test_default_two_field_request_keeps_committing_even_when_unchanged(self):
        controller, runtime, upload, fixture, _ = self.controller()
        result = await controller.refresh({'fixture_id': fixture['id'], 'source_match_id': '12345'})
        self.assertEqual('written', result['reason'])
        self.assertTrue(result['accepted'])
        self.assertTrue(result['snapshot_committed'])
        upload.assert_awaited_once()
        runtime.refresh_match.assert_awaited_once()

    async def test_invalid_fingerprints_fail_before_any_upstream_or_upload(self):
        _, _, known = sample()
        invalid = [None, True, {}, known | {'extra': True}, known | {'context_sha256': 'x'*64},
            known | {'quote_sha256': []}, known | {'quote_sha256': known['quote_sha256']*2},
            known | {'quote_sha256': ['0'*63]}, known | {'quote_sha256': [f'{i:064x}' for i in range(129)]}]
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                controller, runtime, upload, fixture, _ = self.controller()
                with self.assertRaises(ValueError):
                    await controller.refresh({'fixture_id': fixture['id'], 'source_match_id': '12345',
                                               'known_snapshot': value})
                runtime.refresh_match.assert_not_awaited()
                upload.assert_not_awaited()

    async def test_identical_known_requests_share_only_one_source_read(self):
        fixture, observation, known = sample()
        entered, release = asyncio.Event(), asyncio.Event()
        async def fetch(mid):
            entered.set()
            await release.wait()
            return {**fixture, '_current_observation': deepcopy(observation)}
        controller, runtime, upload, _, _ = self.controller(fetch=AsyncMock(side_effect=fetch))
        body = {'fixture_id': fixture['id'], 'source_match_id': '12345', 'known_snapshot': known}
        first = asyncio.create_task(controller.refresh(body))
        await self.started(first, entered)
        second = asyncio.create_task(controller.refresh(deepcopy(body)))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        self.assertTrue(all(result['reason'] == 'observed_unchanged' for result in results))
        runtime.refresh_match.assert_awaited_once()
        upload.assert_not_awaited()
        self.assertEqual({}, controller._inflight)

    async def test_legacy_or_different_proof_cannot_share_an_uncommitted_result(self):
        for legacy in (True, False):
            with self.subTest(legacy=legacy):
                fixture, observation, known = sample()
                entered, release = asyncio.Event(), asyncio.Event()
                async def fetch(mid):
                    entered.set()
                    await release.wait()
                    return {**fixture, '_current_observation': deepcopy(observation)}
                controller, runtime, upload, _, _ = self.controller(fetch=AsyncMock(side_effect=fetch))
                first = asyncio.create_task(controller.refresh({'fixture_id': fixture['id'],
                    'source_match_id': '12345', 'known_snapshot': known}))
                await self.started(first, entered)
                other = {'fixture_id': fixture['id'], 'source_match_id': '12345'}
                if not legacy:
                    other['known_snapshot'] = known | {'context_sha256': '0'*64}
                result = await controller.refresh(other)
                self.assertEqual('refresh_busy', result['reason'])
                self.assertNotIn('current_observation', result)
                release.set()
                self.assertEqual('observed_unchanged', (await first)['reason'])
                runtime.refresh_match.assert_awaited_once()
                upload.assert_not_awaited()

    async def test_cancelled_waiter_does_not_cancel_an_identical_surviving_request(self):
        fixture, observation, known = sample()
        entered, release = asyncio.Event(), asyncio.Event()
        async def fetch(mid):
            entered.set()
            await release.wait()
            return {**fixture, '_current_observation': deepcopy(observation)}
        controller, _, upload, _, _ = self.controller(fetch=AsyncMock(side_effect=fetch))
        body = {'fixture_id': fixture['id'], 'source_match_id': '12345', 'known_snapshot': known}
        first = asyncio.create_task(controller.refresh(body))
        await self.started(first, entered)
        second = asyncio.create_task(controller.refresh(deepcopy(body)))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        self.assertEqual('observed_unchanged', (await second)['reason'])
        upload.assert_not_awaited()
        self.assertEqual({}, controller._inflight)


class QuoteWitnessContractTests(unittest.TestCase):
    def test_missing_or_empty_fingerprint_never_matches_an_observation(self):
        from quote_witness import snapshot_matches
        fixture, observation, known = sample()
        for value in (None, {}, known | {'quote_sha256': []},
                      known | {'quote_sha256': known['quote_sha256']*2}):
            with self.subTest(value=value):
                self.assertFalse(snapshot_matches(value, observation,
                    fixture_id=fixture['id'], source_match_id='12345'))

    def test_database_decimal_and_native_wire_forms_have_identical_fingerprints(self):
        from quote_witness import known_quote_snapshot
        fixture, observation, expected = sample()
        self.assertEqual(expected, known_quote_snapshot(fixture, fixture['markets']))
        wire = deepcopy(observation['quotes'][0])
        wire.update(line='-0.500', price='1.9500',
                    source_updated_at=wire['source_updated_at'].isoformat().replace('+00:00', 'Z'))
        self.assertEqual(expected, known_quote_snapshot(fixture, [wire]))

    def test_unknown_incomplete_or_oversized_quotes_do_not_request_the_fast_path(self):
        from quote_witness import known_quote_snapshot
        fixture, _, _ = sample()
        quote = fixture['markets'][0]
        for rows in ([], [quote]*129, [quote | {'source_updated_at': None}],
                     [quote | {'play_id': ''}], [quote | {'status': 'closed'}]):
            with self.subTest(count=len(rows)):
                self.assertIsNone(known_quote_snapshot(fixture, rows))
