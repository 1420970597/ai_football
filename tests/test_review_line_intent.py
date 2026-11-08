"""Source line conflicts must not become a different accepted market."""
from decimal import Decimal
import os
import unittest

import test_reviews as support


class SourceLineIntentTests(unittest.TestCase):
    def reject(self, text, *, line='0.5', selection='home', kind='handicap', native=True):
        from app.review_line_intent import line_intent_rejection
        quote = {'fixture_id': 'leyu:5707789' if native else 'isolated-fixture',
                 'market_id': '4', 'market_kind': kind, 'period': 'full_time',
                 'line': Decimal(line), 'selection': selection}
        return line_intent_rejection(text, quote)

    def test_actual_confirmed_parma_image_evidence_rejects_half_line(self):
        text = '注单已确认截图：帕爾馬 +0.5/1 @1.96，全场让球[欧洲盘]，帕爾馬 VS 博洛尼亞，11-08 01:00(GMT+8)'
        self.assertEqual('source_line_intent_conflict', self.reject(text))
        self.assertIsNone(self.reject(text, line='.75'))

    def test_signed_asian_split_uses_decimal_mean(self):
        for text, line in [('Alpha +0/0.5', '.25'), ('Alpha -0.5/1', '-.75'),
                           ('Alpha -0/-0.5', '-.25'), ('Alpha +1/1.5', '1.25'),
                           ('Alpha ＋０．５／１', '.75'), ('Alpha −0.5/1', '-.75'),
                           ('帕爾馬+0.5/1', '.75')]:
            with self.subTest(text=text):
                self.assertIsNone(self.reject(text, line=line))
                self.assertEqual('source_line_intent_conflict', self.reject(text, line='3'))

    def test_away_selection_uses_opposite_of_native_home_line(self):
        self.assertIsNone(self.reject('Beta -0.5/1', line='.75', selection='away'))
        self.assertEqual('source_line_intent_conflict',
                         self.reject('Beta +0.5/1', line='.75', selection='away'))
        self.assertIsNone(self.reject('Beta +0.5/1', line='.75', selection='away', native=False))

    def test_explicit_chinese_giving_and_receiving(self):
        for text, line in [('Alpha 受讓0.75', '.75'), ('Alpha 受让 0.5/1', '.75'),
                           ('Beta 让0.75', '-.75'), ('Beta 讓 0.5/1', '-.75'),
                           ('帕爾馬受讓0.5/1', '.75')]:
            with self.subTest(text=text):
                self.assertIsNone(self.reject(text, line=line))
                self.assertEqual('source_line_intent_conflict', self.reject(text, line='2'))

    def test_odds_dates_amounts_and_timezone_are_not_handicap_lines(self):
        for text in ['@1.96 11-08 01:00(GMT+8)', '盈利 +2u，比分1-0',
                     '奖金 +100元，盘口待定', 'Alpha @+150', '2026-11-08T01:00:00+08:00',
                     '赔率 +2.00', 'ODDS +2.00']:
            with self.subTest(text=text):
                self.assertIsNone(self.reject(text))

    def test_ambiguous_or_invalid_evidence_does_not_invent_one_line(self):
        for text in ['Alpha +.5，Beta -.5', 'Alpha +0.5/1.5', 'Alpha +0.3',
                     'Alpha 赢了', '全场让球，截图线不可读', 'Alpha +0.5/1/1.5', 'Alpha +0.5-1']:
            with self.subTest(text=text):
                self.assertIsNone(self.reject(text, line='3'))

    def test_total_line_and_direction_are_checked_without_comparing_odds(self):
        self.assertEqual('source_line_intent_conflict',
                         self.reject('大1.5/2 @1.96', kind='total', selection='over', line='1.5'))
        self.assertIsNone(self.reject('大1.5/2 @9.99', kind='total', selection='over', line='1.75'))
        self.assertEqual('source_selection_intent_conflict',
                         self.reject('小1.5/2', kind='total', selection='over', line='1.75'))

    def test_independent_or_unsupported_intents_are_not_guessed(self):
        self.assertIsNone(self.reject('大2.5、小3.5', kind='total', selection='over', line='4'))
        self.assertIsNone(self.reject('全场大小2.5', kind='total', selection='over', line='2.5'))
        self.assertIsNone(self.reject('Alpha +0.75', kind='winner', line='0'))

    def test_adjacent_total_alternatives_are_not_mistaken_for_one_line(self):
        for line in ('.75','.5'):
            self.assertIsNone(self.reject('半大0.75 0.5', kind='total', selection='over', line=line))
        self.assertEqual('source_line_intent_conflict',
                         self.reject('半大0.75 0.5', kind='total', selection='over', line='1'))
        self.assertEqual('source_line_intent_conflict',
                         self.reject('大2.5 3u', kind='total', selection='over', line='3'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class SourceLineRecordingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture
    search_all = support.ReviewTests.search_all
    resolve = support.ReviewTests.resolve
    read_market = support.ReviewTests.read_market

    async def prepare(self, *, line='0.5', selection='home', kind='handicap', native=True):
        fixture = await self.fixture(id='leyu:5707789' if native else 'fixture-1', markets=[{
            'market_id':'4', 'market_name':'全场让球' if kind=='handicap' else '全场大小',
            'kind':kind, 'period':'full_time', 'line':line, 'selection':selection, 'price':'2.21',
            'source_updated_at':self.now.isoformat(), 'received_at':self.now.isoformat()}])
        key, _ = await self.search_all()
        await self.resolve(key)
        return fixture, await self.read_market(fixture)

    async def record(self, fixture, quote, evidence):
        response = await self.post('record_expert_signals', signals=[{
            'fixture_id':fixture, 'quote_id':quote['quote_id'], 'evidence_text':evidence}])
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    async def assert_rejected_without_persistence(self, result, reason):
        self.assertEqual([], result['accepted'])
        self.assertEqual(reason, result['rejected'][0]['reason'])
        for table in ('v2_expert_signals','v2_quote_evidence','v2_bet_orders'):
            self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM '+table))

    async def test_actual_image_line_cannot_be_recorded_as_neighbor(self):
        fixture, quote = await self.prepare()
        result = await self.record(fixture, quote, '帕爾馬 +0.5/1 @1.96，全场让球')
        await self.assert_rejected_without_persistence(result, 'source_line_intent_conflict')

    async def test_exact_split_line_accepts_system_price_despite_expert_odds(self):
        fixture, quote = await self.prepare(line='.75')
        result = await self.record(fixture, quote, '帕爾馬 +0.5/1 @1.96，全场让球')
        self.assertEqual([], result['rejected'])
        self.assertEqual(('0.750','2.21000'), (result['accepted'][0]['line'],result['accepted'][0]['price']))

    async def test_native_away_line_sign_is_not_reversed_twice(self):
        fixture, quote = await self.prepare(line='.75', selection='away')
        result = await self.record(fixture, quote, 'Beta +0.5/1')
        await self.assert_rejected_without_persistence(result, 'source_line_intent_conflict')
        result = await self.record(fixture, quote, 'Beta -0.5/1')
        self.assertEqual(1, len(result['accepted']))

    async def test_total_source_line_and_direction_conflicts_do_not_persist(self):
        fixture, quote = await self.prepare(line='1.5', selection='over', kind='total', native=False)
        result = await self.record(fixture, quote, '大1.5/2')
        await self.assert_rejected_without_persistence(result, 'source_line_intent_conflict')
        result = await self.record(fixture, quote, '小1.5')
        await self.assert_rejected_without_persistence(result, 'source_selection_intent_conflict')
