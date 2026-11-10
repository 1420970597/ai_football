"""A visual handicap cannot bypass its immutable source period declaration."""
import os
import unittest
import uuid
from decimal import Decimal

from app.review_period_intent import premarket_period_rejection, period_intent_key
import test_premarket_visual_period as visual


class HandicapSourcePeriodTests(unittest.TestCase):
    def quote(self, **changes):
        return {'market_kind': 'handicap', 'period': 'full_time',
                'line': Decimal('-1.5'), 'selection': 'home'} | changes

    def test_empty_visual_scope_rejects_handicap_for_each_period(self):
        scope = {'algorithm': 'premarket-source-intent-v1', 'source_periods': []}
        for period in ('full_time', 'first_half', 'second_half'):
            with self.subTest(period=period):
                self.assertEqual('source_period_unverified_before_market_read',
                    premarket_period_rejection('普利茅斯-1.5', self.quote(period=period),
                                               scope, has_visual_source=True))

    def test_confirmed_source_period_retains_legitimate_handicap(self):
        for periods in (['full_time'], ['first_half', 'full_time']):
            with self.subTest(periods=periods):
                self.assertIsNone(premarket_period_rejection('', self.quote(),
                    {'algorithm': 'premarket-source-intent-v1', 'source_periods': periods},
                    has_visual_source=True))

    def test_other_declared_period_cannot_authorize_handicap(self):
        self.assertEqual('source_period_unverified_before_market_read',
            premarket_period_rejection('', self.quote(),
                {'algorithm': 'premarket-source-intent-v1', 'source_periods': ['first_half']},
                has_visual_source=True))

    def test_nonvisual_path_remains_unchanged(self):
        self.assertIsNone(premarket_period_rejection('普利茅斯-1.5', self.quote(),
            {'algorithm': 'premarket-source-intent-v1', 'source_periods': []}))

    def test_handicap_denial_key_retains_kind_period_line_and_selection(self):
        base = period_intent_key(self.quote())
        self.assertEqual('handicap:full_time:-1.5:home', base)
        self.assertEqual(base, period_intent_key(self.quote(line=Decimal('-1.500'))))
        for changes in ({'period': 'first_half'}, {'line': Decimal('-1')},
                        {'selection': 'away'}, {'market_kind': 'total'}):
            with self.subTest(changes=changes):
                self.assertNotEqual(base, period_intent_key(self.quote(**changes)))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class HandicapSourcePeriodApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = visual.PremarketVisualPeriodTests.asyncSetUp
    asyncTearDown = visual.PremarketVisualPeriodTests.asyncTearDown
    context = visual.PremarketVisualPeriodTests.context
    post = visual.PremarketVisualPeriodTests.post
    fixture = visual.PremarketVisualPeriodTests.fixture
    search_all = visual.PremarketVisualPeriodTests.search_all
    resolve = visual.PremarketVisualPeriodTests.resolve
    read = visual.PremarketVisualPeriodTests.read
    record = visual.PremarketVisualPeriodTests.record
    declaration = visual.PremarketVisualPeriodTests.declaration

    async def prepare(self):
        await self.pool.execute(
            "UPDATE v2_expert_messages SET raw_text='普利茅斯-1.5',media_state='ready',media_objects=$2 WHERE id=$1",
            self.message, [{'sha256': 'c' * 64, 'mime_type': 'image/jpeg',
                            'object_key': 'isolated-handicap-original.jpg'}])
        markets = [{'market_id': 'handicap-' + period, 'market_name': '让球',
                    'kind': 'handicap', 'period': period, 'line': '-1.5',
                    'selection': 'home', 'price': '1.95',
                    'source_updated_at': self.now.isoformat(), 'received_at': self.now.isoformat()}
                   for period in ('full_time', 'first_half')]
        fixture = await self.fixture(markets=markets)
        key, _ = await self.search_all()
        await self.resolve(key)
        return fixture

    async def test_unknown_period_denies_handicap_and_keeps_denial_on_resubmission(self):
        fixture = await self.prepare()
        quotes = await self.read(fixture, market_kind='handicap', source_periods=[])
        self.assertEqual(2, len(quotes))
        before = await self.declaration(fixture)
        for evidence in ('原图普利茅斯-1.5', '原图全场普利茅斯-1.5'):
            result = await self.record(fixture, quotes, evidence)
            self.assertEqual([], result['accepted'])
            self.assertEqual(['source_period_unverified_before_market_read'] * 2,
                             [x['reason'] for x in result['rejected']])
        self.assertEqual(before, await self.declaration(fixture))
        denials = await self.pool.fetchval(
            "SELECT evidence->'source_period_denials' FROM v2_review_fixture_bindings "
            "WHERE job_id=$1 AND attempt_id=$2 AND fixture_id=$3", self.job, self.attempt, fixture)
        self.assertEqual({'handicap:full_time:-1.5:home': 'source_period_intent_ambiguous',
                          'handicap:first_half:-1.5:home': 'source_period_intent_ambiguous'}, denials)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_predeclared_half_accepts_half_and_rejects_full(self):
        fixture = await self.prepare()
        quotes = await self.read(fixture, market_kind='handicap', source_periods=['first_half'])
        half = next(q for q in quotes if q['period'] == 'first_half')
        full = next(q for q in quotes if q['period'] == 'full_time')
        result = await self.record(fixture, [full, half], '原图普利茅斯-1.5')
        self.assertEqual(1, len(result['accepted']))
        self.assertEqual('first_half', result['accepted'][0]['period'])
        self.assertEqual('1.95000', result['accepted'][0]['price'])
        self.assertEqual('source_period_unverified_before_market_read', result['rejected'][0]['reason'])

    async def test_predeclared_multiple_periods_preserve_both_recommendations(self):
        fixture = await self.prepare()
        quotes = await self.read(fixture, market_kind='handicap',
                                 source_periods=['full_time', 'first_half'])
        result = await self.record(fixture, quotes, '原图全场和上半场分别选择普利茅斯-1.5')
        self.assertEqual([], result['rejected'])
        self.assertEqual(2, len(result['accepted']))
        self.assertEqual({'1.95000'}, {item['price'] for item in result['accepted']})

    async def test_later_read_cannot_expand_unknown_source_period(self):
        fixture = await self.prepare()
        await self.read(fixture, market_kind='handicap', source_periods=[])
        first = await self.declaration(fixture)
        full, = await self.read(fixture, market_kind='handicap', period='full_time',
                                source_periods=['full_time'])
        result = await self.record(fixture, [full], '原图全场普利茅斯-1.5')
        self.assertEqual([], result['accepted'])
        self.assertEqual('source_period_unverified_before_market_read', result['rejected'][0]['reason'])
        self.assertEqual(first, await self.declaration(fixture))

    async def test_new_attempt_cannot_erase_prior_period_denial(self):
        fixture = await self.prepare()
        full, = await self.read(fixture, market_kind='handicap', period='full_time', source_periods=[])
        result = await self.record(fixture, [full], '原图普利茅斯-1.5')
        self.assertEqual([], result['accepted'])
        self.attempt = uuid.uuid4()
        await self.pool.execute(
            "UPDATE v2_analysis_jobs SET attempt_id=$2,lease_until=now()+interval '5 minutes' WHERE id=$1",
            self.job, self.attempt)
        key, _ = await self.search_all(team='Beta')
        await self.resolve(key)
        full, = await self.read(fixture, market_kind='handicap', period='full_time',
                                source_periods=['full_time'])
        result = await self.record(fixture, [full], '原图全场普利茅斯-1.5')
        self.assertEqual([], result['accepted'])
        self.assertEqual('source_period_intent_ambiguous', result['rejected'][0]['reason'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
