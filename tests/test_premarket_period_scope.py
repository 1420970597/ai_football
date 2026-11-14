"""Commit source periods before inventory; retain legitimate multi-period images."""
import os
import unittest
from decimal import Decimal

import test_premarket_visual_period as visual
from app.review_period_intent import premarket_period_rejection


class PremarketPeriodScopeTests(unittest.TestCase):
    def reject(self, declaration, text='破蛋', **changes):
        quote={'market_kind':'total','period':'first_half','line':Decimal('.5'),
               'selection':'over'} | changes
        return premarket_period_rejection(text,quote,
            {'algorithm':'premarket-source-intent-v1',**declaration},has_visual_source=True)

    def test_first_full_period_cannot_be_replaced_by_half_inventory(self):
        self.assertEqual('source_period_unverified_before_market_read',
                         self.reject({'period':'full_time'}))

    def test_explicit_empty_scope_is_unknown_even_when_read_filter_has_period(self):
        self.assertEqual('source_period_unverified_before_market_read',
                         self.reject({'period':'first_half','source_periods':[]}))

    def test_predeclared_single_and_multiple_periods_remain_usable(self):
        for scope in ({'period':'first_half'},
                      {'period':'full_time','source_periods':['full_time','first_half']}):
            with self.subTest(scope=scope):self.assertIsNone(self.reject(scope))

    def test_explicit_source_text_is_per_pick_and_does_not_need_read_filter_agreement(self):
        scope={'period':'full_time','source_periods':[]}
        self.assertIsNone(self.reject(scope,'上半场0.5大'))
        for text in ('上半场0.5小','上半场1.5大','全场0.5大'):
            with self.subTest(text=text):self.assertIsNotNone(self.reject(scope,text))

    def test_other_markets_do_not_gain_or_lose_period_authority(self):
        self.assertIsNone(self.reject({'period':'full_time'},market_kind='winner'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated PostgreSQL')
class PremarketPeriodScopeApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=visual.PremarketVisualPeriodTests.asyncSetUp
    asyncTearDown=visual.PremarketVisualPeriodTests.asyncTearDown
    context=visual.PremarketVisualPeriodTests.context
    post=visual.PremarketVisualPeriodTests.post
    fixture=visual.PremarketVisualPeriodTests.fixture
    search_all=visual.PremarketVisualPeriodTests.search_all
    resolve=visual.PremarketVisualPeriodTests.resolve
    prepare=visual.PremarketVisualPeriodTests.prepare
    read=visual.PremarketVisualPeriodTests.read
    record=visual.PremarketVisualPeriodTests.record
    declaration=visual.PremarketVisualPeriodTests.declaration

    async def assert_denied(self,result):
        self.assertEqual([],result['accepted'])
        self.assertEqual('source_period_unverified_before_market_read',result['rejected'][0]['reason'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*)FROM v2_expert_signals'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*)FROM v2_bet_orders'))

    async def test_original_native_full_then_half_without_an_admission_is_denied(self):
        fixture=await self.prepare()
        await self.read(fixture,market_kind='total',period='full_time')
        half=next(q for q in await self.read(fixture)if q['period']=='first_half')
        await self.assert_denied(await self.record(fixture,[half],
            '原文破蛋，配图21分钟0-0，属有进球看大，对应上半场大0.5。'))

    async def test_explicit_unknown_scope_does_not_borrow_a_concrete_read_period(self):
        fixture=await self.prepare()
        half,=await self.read(fixture,period='first_half',source_periods=[])
        await self.assert_denied(await self.record(fixture,[half],'上半场大0.5'))

    async def test_multiple_image_periods_declared_before_inventory_are_both_recorded(self):
        fixture=await self.prepare(text='')
        await self.read(fixture,period='full_time',source_periods=['full_time','first_half'])
        quotes=await self.read(fixture)
        result=await self.record(fixture,quotes,'两张原图分别选中全场大2.5、上半场大0.5，倍率不参与')
        self.assertEqual([],result['rejected']);self.assertEqual(2,len(result['accepted']))
        self.assertEqual({'1.95000'},{r['price']for r in result['accepted']})

    async def test_later_scope_extension_cannot_rewrite_the_first_declaration(self):
        fixture=await self.prepare()
        await self.read(fixture,period='full_time',source_periods=['full_time'])
        first=await self.declaration(fixture)
        half,=await self.read(fixture,period='first_half',source_periods=['full_time','first_half'])
        self.assertEqual(first,await self.declaration(fixture))
        await self.assert_denied(await self.record(fixture,[half],'原图所选上半场大0.5'))

    async def test_explicit_original_half_is_not_suppressed_by_unknown_source_scope(self):
        fixture=await self.prepare(text='上半场0.5大')
        half,=await self.read(fixture,period='first_half',source_periods=[])
        result=await self.record(fixture,[half],'上半场0.5大')
        self.assertEqual([],result['rejected']);self.assertEqual(1,len(result['accepted']))

    async def test_null_explicit_scope_is_unknown(self):
        fixture=await self.prepare()
        half,=await self.read(fixture,period='first_half',source_periods=None)
        await self.assert_denied(await self.record(fixture,[half],'原图所选上半场大0.5'))

    async def test_invalid_scope_rejected_before_declaring_or_reading(self):
        fixture=await self.prepare()
        for scope in (['quarter'],['full_time']*4,'first_half'):
            with self.subTest(scope=scope):
                response=await self.post('list_match_markets',fixture_id=fixture,source_periods=scope)
                self.assertEqual(422,response.status_code)
                self.assertIsNone(await self.declaration(fixture))

    async def test_period_switch_denial_is_retained_before_a_retry(self):
        fixture=await self.prepare()
        await self.read(fixture,period='full_time')
        half=next(q for q in await self.read(fixture)if q['period']=='first_half')
        await self.assert_denied(await self.record(fixture,[half],'原图所选上半场大0.5'))
        denial=await self.pool.fetchval("SELECT evidence->'source_period_denials' FROM v2_review_fixture_bindings WHERE job_id=$1 AND attempt_id=$2 AND fixture_id=$3",self.job,self.attempt,fixture)
        self.assertEqual({'total:first_half:0.5:over':'source_period_intent_ambiguous'},denial)
