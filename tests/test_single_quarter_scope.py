"""Synthetic captions only; no historical expert text or images."""
import os
import unittest
from unittest.mock import AsyncMock, patch

from app.review_period_intent import (single_quarter_source, single_quarter_source_notice,
                                      period_intent_rejection)
from app import hermes_client
import test_bare_total_source_context as inputs
import test_handicap_source_period as handicap


class SingleQuarterScopeTests(unittest.TestCase):
    def test_explicit_quarter_captions_never_gain_whole_or_half_authority(self):
        for text in ('第四节+2.5', '第4節 受让+2.5', '末节：主队-2.5', '第三节大45.5'):
            self.assertTrue(single_quarter_source(text))
            self.assertIn('不要声明full_time', single_quarter_source_notice(text))
            for period in ('full_time', 'first_half', 'second_half'):
                self.assertEqual('source_period_not_supported', period_intent_rejection(
                    text, {'fixture_sport': 'basketball', 'market_kind': 'handicap', 'period': period}))

    def test_independent_scopes_history_stage_and_non_basketball_are_preserved(self):
        for text in ('全场主队-2.5', '上半场大45.5', '第四节开始了，主队-2.5',
                     '昨天第四节+2.5', '第四节+2.5？', '第四节+2.5\n全场-2.5',
                     '第四节+2.5；上半场大45.5', '', None):
            self.assertFalse(single_quarter_source(text))
            self.assertEqual('', single_quarter_source_notice(text))
        self.assertIsNone(period_intent_rejection('第四节+2.5',
            {'fixture_sport': 'football', 'market_kind': 'handicap', 'period': 'full_time'}))


class QuarterPreparedInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_notice_preserves_structured_image_and_original_timestamp(self):
        image, message = inputs.BareTotalPreparedInputTests.message(self, '第四节+2.5')
        with patch.object(hermes_client.blobstore, 'read_image', AsyncMock(return_value=image)):
            parts = await hermes_client._content_parts(message, 'isolated-review', 'isolated-attempt')
        self.assertIn('来源单节期间约束', parts[0]['text'])
        self.assertIn('第四节+2.5', parts[0]['text'])
        self.assertIn('2026-11-10T10:24:28+00:00', parts[0]['text'])
        self.assertEqual(1, sum(p['type'] == 'image_url' for p in parts))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class QuarterApiTests(handicap.HandicapSourcePeriodApiTests):
    async def test_quarter_caption_does_not_expose_whole_or_half_markets(self):
        fixture = await self.prepare()
        await self.pool.execute("UPDATE v2_fixtures SET sport='basketball' WHERE id=$1", fixture)
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2 WHERE id=$1', self.message, '第四节+2.5')
        for period in ('full_time', 'first_half', 'second_half'):
            response = await self.post('list_match_markets', fixture_id=fixture, period=period,
                                       source_periods=[period], market_kind='handicap')
            self.assertEqual(200, response.status_code)
            result = response.json()
            self.assertEqual([], result['markets'])
            self.assertEqual('source_period_not_supported', result['reason'])
            self.assertFalse(result['rejection_guidance']['retry_same_evidence'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_previously_read_quote_cannot_bypass_quarter_guard(self):
        fixture = await self.prepare()
        quotes = await self.read(fixture, market_kind='handicap', source_periods=['full_time'])
        await self.pool.execute("UPDATE v2_fixtures SET sport='basketball' WHERE id=$1", fixture)
        # Refresh the read context to isolate source-period enforcement.
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2 WHERE id=$1', self.message, '第四节-1.5')
        result = await self.record(fixture, quotes, '主队-1.5')
        self.assertEqual([], result['accepted'])
        self.assertTrue(all(x['reason'] == 'source_period_not_supported' for x in result['rejected']))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))
