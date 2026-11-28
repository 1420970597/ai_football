"""Unsupported signed source expressions cannot masquerade as missing inventory."""
import os
import unittest

import test_reviews as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class HandicapQueryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture
    search_all = support.ReviewTests.search_all
    resolve = support.ReviewTests.resolve

    async def prepare(self):
        fixture = await self.fixture(1, id='leyu:98886007', home_team='圣克拉拉U23',
            away_team='图伦斯U23', markets=[dict(market_id='4', market_name='全场让球',
            kind='handicap', period='full_time', line='.25', selection='away', price='1.95',
            source_updated_at=self.now.isoformat(), received_at=self.now.isoformat())])
        key, _ = await self.search_all('圣克拉拉')
        result = await self.post('resolve_match', search_key=key, team_a='圣克拉拉U23', team_b='图伦斯U23')
        self.assertTrue(result.json()['resolved'])
        return fixture

    async def test_original_signed_team_query_returns_explicit_recovery_then_correct_away_line(self):
        fixture = await self.prepare()
        response = await self.post('list_match_markets', fixture_id=fixture, market_kind='handicap',
            period='full_time', market_query='图伦斯 u23 -0/0.5')
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual('handicap_market_query_requires_structured_filter', response.json()['detail']['reason'])
        self.assertTrue(response.json()['detail']['retry_without_market_query'])
        read = await self.post('list_match_markets', fixture_id=fixture, market_kind='handicap', period='full_time')
        self.assertEqual(200, read.status_code, read.text)
        rows = read.json()['markets']
        self.assertEqual(1, len(rows))
        self.assertEqual('away', rows[0]['selection'])
        self.assertEqual('-0.250', rows[0]['selection_line'])
        self.assertEqual('0.250', rows[0]['line'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_unknown_attempt_does_not_gain_market_or_signal_access(self):
        fixture = await self.prepare()
        import uuid
        response = await self.post('list_match_markets', fixture_id=fixture,
            attempt_id=str(uuid.uuid4()), market_kind='handicap', period='full_time')
        self.assertEqual(409, response.status_code, response.text)

    async def test_plain_market_label_keeps_existing_name_filter(self):
        fixture = await self.prepare()
        response = await self.post('list_match_markets', fixture_id=fixture,
            market_kind='handicap', period='full_time', market_query='全场让球')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, len(response.json()['markets']))
