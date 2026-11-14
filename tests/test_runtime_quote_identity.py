"""WS batches preserve identities and source ordering without duplicate delivery."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

import httpx

from services.collector.leyu_runtime import LeyuRuntime


class RuntimeQuoteIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = httpx.AsyncClient()
        self.runtime = LeyuRuntime(self.api, 'http://isolated.invalid/internal')
        self.runtime.fixtures = {'leyu:one': {
            'id': 'leyu:one', 'source_match_id': 'one', 'phase': 'live',
            'markets': [], 'markets_complete': True,
        }}

    async def asyncTearDown(self):
        await self.api.aclose()

    def quote(self, step=0, **changes):
        timestamp = datetime(2026, 10, 27, 12, tzinfo=timezone.utc) + timedelta(seconds=step)
        return {'market_id': 'winner', 'market_kind': 'winner', 'period': 'full_time',
                'line': None, 'selection': 'home', 'price': '2.00', 'status': 'active',
                'market_instance_id': 'instance', 'selection_id': 'option',
                'source_updated_at': timestamp.isoformat(), 'source_time_quality': 'known',
                'status_evidence': True, **changes}

    def push(self, *quotes):
        return self.runtime.apply_push([{'fixture_id': 'leyu:one', 'markets': [quote]} for quote in quotes])

    async def test_501_updates_of_one_key_deliver_only_latest_coherent_quote(self):
        quotes = [self.quote(step, price=str(Decimal('2') + Decimal(step) / 1000),
                             status='suspended' if step == 500 else 'active') for step in range(501)]
        output, resync = self.push(*quotes)
        self.assertFalse(resync)
        self.assertEqual(1, len(output))
        self.assertFalse(output[0]['markets_complete'])
        self.assertEqual(1, len(output[0]['markets']))
        latest = output[0]['markets'][0]
        self.assertEqual(('2.5', 'suspended', quotes[-1]['source_updated_at']),
                         (latest['price'], latest['status'], latest['source_updated_at']))
        self.assertEqual(1, len(self.runtime.fixtures['leyu:one']['markets']))
        self.assertEqual(501, self.runtime._push_revision)
        self.assertEqual(501, next(iter(self.runtime._market_push_revisions.values())))

    async def test_distinct_501_identities_are_all_preserved_without_truncation(self):
        quotes = [self.quote(step, market_id=f'market-{step}') for step in range(501)]
        output, resync = self.push(*quotes)
        self.assertFalse(resync)
        self.assertEqual(501, len(output[0]['markets']))
        self.assertEqual({q['market_id'] for q in quotes}, {q['market_id'] for q in output[0]['markets']})
        self.assertEqual(501, len(self.runtime._market_push_revisions))

    async def test_old_or_unknown_events_cannot_replace_accepted_price_state_or_revision(self):
        newest = self.quote(20, price='2.20', status='closed')
        output, resync = self.push(newest, self.quote(1, price='9.99'),
                                   self.quote(30, source_updated_at=None))
        self.assertTrue(resync)
        self.assertEqual(1, len(output[0]['markets']))
        self.assertEqual(('2.20', 'closed', newest['source_updated_at']),
                         tuple(output[0]['markets'][0][field] for field in ('price', 'status', 'source_updated_at')))
        self.assertEqual(1, self.runtime._push_revision)

    async def test_status_without_evidence_stays_suspended_and_replay_does_not_bump_revision(self):
        self.push(self.quote(status='suspended'))
        update = self.quote(1, price='2.10', status='active', status_evidence=False)
        output, resync = self.push(update, update)
        self.assertFalse(resync)
        self.assertEqual('suspended', output[0]['markets'][0]['status'])
        self.assertEqual(2, self.runtime._push_revision)
        self.assertEqual(([], False), self.push(update))
        self.assertEqual(2, self.runtime._push_revision)

    async def test_zero_aliases_only_apply_to_explicit_winner(self):
        winner = self.runtime._market_key(self.quote())
        for line in (0, '0', Decimal('0'), '0.00', Decimal('-0.0')):
            with self.subTest(line=line):
                self.assertEqual(winner, self.runtime._market_key(self.quote(line=line)))
        for kind in ('handicap', 'total', None):
            self.assertNotEqual(self.runtime._market_key(self.quote(market_kind=kind, line=None)),
                                self.runtime._market_key(self.quote(market_kind=kind, line=0)))
        for invalid in (False, '', 'invalid', 'NaN'):
            self.assertNotEqual(winner, self.runtime._market_key(self.quote(line=invalid)))

    async def test_old_winner_zero_and_new_null_share_identity_with_source_time_ordering(self):
        for old_line in (0, '0', Decimal('0')):
            with self.subTest(old_line=old_line):
                runtime = LeyuRuntime(self.api, 'http://isolated.invalid/internal')
                old = self.quote(0, line=old_line)
                old.pop('status_evidence')
                runtime.fixtures = {'leyu:one': {'id': 'leyu:one', 'markets': [old]}}
                latest = self.quote(5, price='2.50')
                output, _ = runtime.apply_push([{'fixture_id': 'leyu:one', 'markets': [latest, self.quote(1, line=old_line)]}])
                self.assertEqual(1, len(output[0]['markets']))
                self.assertIsNone(output[0]['markets'][0]['line'])
                self.assertEqual('2.50', output[0]['markets'][0]['price'])
                self.assertEqual(1, runtime._push_revision)
                incoming = {'id': 'leyu:one', 'markets': [deepcopy(old)], 'markets_complete': True}
                runtime._merge_fixture(incoming, runtime.fixtures['leyu:one'], runtime._push_revision)
                self.assertEqual(1, len(incoming['markets']))
                self.assertIsNone(incoming['markets'][0]['line'])
                self.assertEqual('2.50', incoming['markets'][0]['price'])

    async def test_offset_source_times_are_compared_as_instants(self):
        output, resync = self.push(self.quote(10), self.quote(0, source_updated_at='2026-10-27T13:00:05+01:00', price='9.99'))
        self.assertFalse(resync)
        self.assertEqual('2.00', output[0]['markets'][0]['price'])
        self.assertEqual(1, self.runtime._push_revision)


if __name__ == '__main__':
    unittest.main()
