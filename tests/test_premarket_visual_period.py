"""An image's unknown period cannot be replaced by the offered inventory."""
import asyncio
import os
import unittest
from decimal import Decimal

import test_reviews as support
from app.review_line_intent import source_has_explicit_total_line
from app.review_period_intent import premarket_period_rejection


class PremarketSourcePresenceTests(unittest.TestCase):
    def test_line_presence_handles_independent_directions_without_odds_or_scores(self):
        for text in ('大0.5','1.5大','上半0.75大 大1.5','大2.5 小3.5','OVER 0.5','under 1.5/2'):
            with self.subTest(text=text):self.assertTrue(source_has_explicit_total_line(text))
        for text in ('破蛋','0-0 第21分钟','赔率1.53','大一点 3u','大小2.5','大0.5/1.5'):
            with self.subTest(text=text):self.assertFalse(source_has_explicit_total_line(text))

    def test_unknown_visual_period_is_independent_of_explicit_line_presence(self):
        quote={'market_kind':'total','line':Decimal('1.5'),'selection':'over','period':'first_half'}
        declaration={'algorithm':'premarket-source-intent-v1','period':None}
        for text in ('大1.5','1.5大','OVER 1.5','破蛋'):
            with self.subTest(text=text):
                self.assertEqual('source_period_unverified_before_market_read',
                    premarket_period_rejection(text,quote,declaration,has_visual_source=True))

    def test_known_visual_period_nonvisual_and_other_markets_keep_existing_path(self):
        quote={'market_kind':'total','line':Decimal('1.5'),'selection':'over','period':'first_half'}
        unknown={'algorithm':'premarket-source-intent-v1','period':None}
        self.assertIsNone(premarket_period_rejection('1.5大',quote,unknown))
        self.assertIsNone(premarket_period_rejection('1.5大',quote,
            {**unknown,'period':'first_half'},has_visual_source=True))
        self.assertIsNone(premarket_period_rejection('主队+1.5',
            {**quote,'market_kind':'handicap'},unknown,has_visual_source=True))

    def test_text_period_proof_belongs_to_selected_line_and_direction(self):
        quote={'market_kind':'total','line':Decimal('1.5'),'selection':'over','period':'first_half'}
        unknown={'algorithm':'premarket-source-intent-v1','period':None}
        for text in ('上半场大1.5','上半场1.5大'):
            with self.subTest(text=text):
                self.assertIsNone(premarket_period_rejection(text,quote,unknown,has_visual_source=True))
        for text in ('上半场小1.5，1.5大','上半场大0.5，1.5大','全场1.5大'):
            with self.subTest(text=text):
                self.assertEqual('source_period_unverified_before_market_read',
                    premarket_period_rejection(text,quote,unknown,has_visual_source=True))

    def test_rejection_summary_does_not_deny_the_available_numeric_line(self):
        from app.review_summary import make_summary
        result=make_summary([],accepted_total=0,
            rejected=[{'reason':'source_period_unverified_before_market_read'}],review_mode='real_time')
        self.assertNotIn('无明确大小球线值',result['content'])
        self.assertIn('期间未确认',result['content'])


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated PostgreSQL')
class PremarketVisualPeriodTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture
    search_all=support.ReviewTests.search_all
    resolve=support.ReviewTests.resolve

    async def prepare(self,*,text='破蛋'):
        await self.pool.execute('UPDATE v2_expert_messages SET raw_text=$2,media_state=\'ready\',media_objects=$3 WHERE id=$1',self.message,text,[{'sha256':'a'*64,'mime_type':'image/jpeg','object_key':'isolated-score.jpg'}])
        markets=[{'market_id':market,'market_name':name,'kind':'total','period':period,'line':line,
                  'selection':'over','price':'1.95','source_updated_at':self.now.isoformat(),
                  'received_at':self.now.isoformat()}for market,name,period,line in
                 (('18','上半场大小','first_half','0.5'),('2','全场大小','full_time','2.5'))]
        fixture=await self.fixture(markets=markets)
        key,_=await self.search_all();await self.resolve(key)
        return fixture

    async def read(self,fixture,**filters):
        response=await self.post('list_match_markets',fixture_id=fixture,**filters)
        self.assertEqual(200,response.status_code,response.text)
        return response.json()['markets']

    async def declaration(self,fixture):
        return await self.pool.fetchval("SELECT evidence->'source_market_intent' FROM v2_review_fixture_bindings WHERE job_id=$1 AND attempt_id=$2 AND fixture_id=$3",self.job,self.attempt,fixture)

    async def record(self,fixture,quotes,evidence='上半场大0.5，全场大2.5（库存不能作为来源）'):
        r=await self.post('record_expert_signals',signals=[{'fixture_id':fixture,'quote_id':q['quote_id'],'evidence_text':evidence}for q in quotes])
        self.assertEqual(200,r.status_code,r.text)
        return r.json()

    async def test_unknown_first_period_rejects_both_inventory_options(self):
        fixture=await self.prepare();quotes=await self.read(fixture,market_kind='total')
        self.assertEqual(2,len(quotes))
        result=await self.record(fixture,quotes)
        self.assertEqual([],result['accepted'])
        self.assertEqual(['source_period_unverified_before_market_read']*2,[x['reason']for x in result['rejected']])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*)FROM v2_expert_signals'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*)FROM v2_bet_orders'))

    async def test_subsequent_precise_filter_or_fabricated_evidence_cannot_rewrite_first_intent(self):
        fixture=await self.prepare();await self.read(fixture)
        before=await self.declaration(fixture)
        quotes=await self.read(fixture,market_kind='total',period='first_half',line='0.5',selection='over',source_market_intent={'period':'first_half'})
        self.assertEqual(before,await self.declaration(fixture))
        result=await self.record(fixture,quotes,evidence='所选单据上半场大0.5 @9.99')
        self.assertEqual([],result['accepted'])
        self.assertEqual('source_period_unverified_before_market_read',result['rejected'][0]['reason'])

    async def test_empty_first_inventory_still_preserves_the_source_unknown(self):
        fixture=await self.prepare();await self.read(fixture,market_kind='unsupported')
        quotes=await self.read(fixture,period='first_half')
        self.assertIsNone((await self.declaration(fixture))['period'])
        self.assertEqual([], (await self.record(fixture,quotes))['accepted'])

    async def test_source_selected_half_can_declare_period_before_any_market(self):
        fixture=await self.prepare(text='')
        quotes=await self.read(fixture,market_kind='total',period='first_half',line='0.5',selection='over')
        result=await self.record(fixture,quotes,evidence='原图所选上半场大0.5 @1.53')
        self.assertEqual([],result['rejected']);self.assertEqual(1,len(result['accepted']))
        self.assertEqual('1.95000',result['accepted'][0]['price'])
        evidence=await self.pool.fetchval("SELECT evidence FROM v2_quote_evidence WHERE evidence_kind='expert_signal'")
        self.assertEqual(await self.declaration(fixture),evidence['source_market_intent'])

    async def assert_unknown_numeric_period(self,text):
        fixture=await self.prepare(text=text)
        quotes=await self.read(fixture,market_kind='total',line='0.5',selection='over')
        result=await self.record(fixture,quotes,evidence='上半场大0.5 @9.99')
        self.assertEqual([],result['accepted'])
        self.assertEqual('source_period_unverified_before_market_read',result['rejected'][0]['reason'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_concrete_prefix_line_cannot_supply_unknown_period(self):
        await self.assert_unknown_numeric_period('大0.5')

    async def test_concrete_suffix_line_cannot_supply_unknown_period(self):
        await self.assert_unknown_numeric_period('0.5大')

    async def assert_explicit_text_period(self,text):
        fixture=await self.prepare(text=text)
        quotes=await self.read(fixture,market_kind='total',line='0.5',selection='over')
        result=await self.record(fixture,quotes,evidence=text+' @9.99')
        self.assertEqual([],result['rejected'])
        self.assertEqual(1,len(result['accepted']))
        self.assertEqual('1.95000',result['accepted'][0]['price'])

    async def test_explicit_prefix_period_survives_unfiltered_first_read(self):
        await self.assert_explicit_text_period('上半场大0.5')

    async def test_explicit_suffix_period_survives_unfiltered_first_read(self):
        await self.assert_explicit_text_period('上半场0.5大')

    async def test_suffix_query_reads_and_records_exact_declared_half_at_system_price(self):
        fixture=await self.prepare(text='上半场0.5大，倍率9.99')
        quotes=await self.read(fixture,market_kind='total',period='first_half',market_query='0.5大')
        self.assertEqual(1,len(quotes))
        self.assertEqual('first_half',quotes[0]['period'])
        result=await self.record(fixture,quotes,evidence='上半场0.5大 @9.99')
        self.assertEqual([],result['rejected'])
        self.assertEqual(1,len(result['accepted']))
        self.assertEqual('1.95000',result['accepted'][0]['price'])

    async def test_period_from_another_pick_does_not_supply_unknown_pick_period(self):
        fixture=await self.prepare(text='上半场小0.5，2.5大')
        quotes=await self.read(fixture,market_kind='total',period=None,line='2.5',selection='over')
        result=await self.record(fixture,quotes,evidence='全场大2.5')
        self.assertEqual([],result['accepted'])
        self.assertEqual('source_period_unverified_before_market_read',result['rejected'][0]['reason'])

    async def test_explicit_source_text_does_not_depend_on_a_model_omitting_read_filters(self):
        fixture=await self.prepare(text='上半场大0.5，倍率9.99')
        quotes=await self.read(fixture,period='first_half')
        self.assertEqual(1,len((await self.record(fixture,quotes,evidence='上半场大0.5 @9.99'))['accepted']))

    async def test_read_declaration_is_scoped_to_attempt_and_fixture(self):
        fixture=await self.prepare();await self.read(fixture,period='first_half')
        before=await self.declaration(fixture)
        wrong=await self.post('list_match_markets',fixture_id=fixture,attempt_id='00000000-0000-0000-0000-000000000001',period=None)
        self.assertEqual(409,wrong.status_code)
        self.assertEqual(before,await self.declaration(fixture))

    async def test_concurrent_reads_commit_one_immutable_declaration(self):
        fixture=await self.prepare()
        await asyncio.gather(self.read(fixture),self.read(fixture,period='first_half'))
        first=await self.declaration(fixture)
        await self.read(fixture,period='full_time')
        self.assertEqual(first,await self.declaration(fixture))
