"""A model's admitted inventory substitution is denial, never source proof."""
import asyncio
import os
import unittest
import uuid
from decimal import Decimal
from unittest.mock import patch

from app import review_period_intent as period
import test_premarket_visual_period as visual
from app.routes import reviews


ADMISSION = ('原文仅“破蛋”，原文未明示期间与线值；'
             '当前库存只有上半场大小大0.5，按最近似选项录为上半场大0.5。')


class PeriodEvidenceAdmissionTests(unittest.TestCase):
    def quote(self, **changes):
        return {'market_kind':'total','period':'first_half','line':Decimal('.5'),
                'selection':'over'} | changes

    def test_admitted_unknown_period_and_affirmative_inventory_substitution_reject(self):
        for evidence in (ADMISSION,
                         '来源期间未知，根据库存选取上半场大0.5',
                         '原文没有给出期间，按可用盘口推断为上半场大0.5',
                         'Source period is unclear; using the only available first-half option.'):
            with self.subTest(evidence=evidence):
                self.assertEqual('source_period_intent_ambiguous',
                                 period.evidence_period_rejection(evidence,self.quote()))

    def test_image_selected_period_and_rejection_of_inventory_inference_are_not_denials(self):
        for evidence in ('原文未明示期间，但原图明确所选上半场大0.5，按原图录入。',
                         '原文未明示期间，不能按最近似选项录为上半场大0.5。',
                         '原文未明示期间，不是根据库存选择期间，原图所选上半场大0.5。',
                         '原图所选上半场大0.5，系统库存正好有原盘；专家倍率不参与。',
                         '原文上半场大0.5，按库存读取原盘报价。'):
            with self.subTest(evidence=evidence):
                self.assertIsNone(period.evidence_period_rejection(evidence,self.quote()))

    def test_other_market_and_absent_evidence_do_not_invent_a_period(self):
        self.assertIsNone(period.evidence_period_rejection(ADMISSION,self.quote(market_kind='handicap')))
        for text in ('',None,'原文只说破蛋','期间未知，等待来源确认'):
            self.assertIsNone(period.evidence_period_rejection(text,self.quote()))

    def test_native_intent_key_is_numeric_and_does_not_merge_period_direction_or_kind(self):
        base=period.period_intent_key(self.quote())
        self.assertEqual(base,period.period_intent_key(self.quote(line=Decimal('0.500'))))
        for changes in ({'line':Decimal('.75')},{'period':'full_time'},
                        {'selection':'under'},{'market_kind':'team_total'}):
            self.assertNotEqual(base,period.period_intent_key(self.quote(**changes)))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated PostgreSQL')
class PeriodAdmissionApiTests(unittest.IsolatedAsyncioTestCase):
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

    async def original_path(self, text='破蛋'):
        fixture=await self.prepare(text=text)
        self.assertEqual([],await self.read(fixture,market_kind='total',period='full_time',line='0.5',
                                         source_periods=['full_time','first_half']))
        quotes=await self.read(fixture)
        half=next(q for q in quotes if q['period']=='first_half')
        return fixture,quotes,half

    async def assert_denied(self,result):
        self.assertEqual([],result['accepted'])
        self.assertEqual('source_period_intent_ambiguous',result['rejected'][0]['reason'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*)FROM v2_expert_signals'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*)FROM v2_bet_orders'))

    async def test_original_full_then_stock_half_admission_is_rejected(self):
        fixture,_,half=await self.original_path()
        before=await self.declaration(fixture)
        await self.assert_denied(await self.record(fixture,[half],ADMISSION))
        self.assertEqual(before,await self.declaration(fixture))
        marker=await self.pool.fetchval("SELECT evidence->'source_period_denials' FROM v2_review_fixture_bindings WHERE job_id=$1 AND attempt_id=$2 AND fixture_id=$3",self.job,self.attempt,fixture)
        self.assertEqual({period.period_intent_key(half):'source_period_intent_ambiguous'},marker)

    async def test_rewording_same_read_option_cannot_remove_the_denial(self):
        fixture,_,half=await self.original_path()
        await self.assert_denied(await self.record(fixture,[half],ADMISSION))
        await self.assert_denied(await self.record(fixture,[half],'原图所选上半场大0.5'))

    async def test_same_source_job_next_attempt_retains_denial(self):
        fixture,_,half=await self.original_path()
        await self.assert_denied(await self.record(fixture,[half],ADMISSION))
        self.attempt=uuid.uuid4()
        await self.pool.execute("UPDATE v2_analysis_jobs SET attempt_id=$2,lease_until=now()+interval '5 minutes' WHERE id=$1",self.job,self.attempt)
        key,_=await self.search_all(team='Beta');await self.resolve(key)
        half,=await self.read(fixture,market_kind='total',period='first_half',line='0.5')
        await self.assert_denied(await self.record(fixture,[half],'原图所选上半场大0.5'))

    async def test_independent_period_and_line_are_not_suppressed(self):
        fixture,quotes,half=await self.original_path(text='全场大2.5，另看破蛋')
        await self.assert_denied(await self.record(fixture,[half],ADMISSION))
        full=next(q for q in quotes if q['period']=='full_time')
        result=await self.record(fixture,[full],'原文全场大2.5，原倍率9.99不参与')
        self.assertEqual([],result['rejected']);self.assertEqual(1,len(result['accepted']))
        self.assertEqual('1.95000',result['accepted'][0]['price'])

    async def test_new_message_revision_does_not_inherit_the_previous_job_denial(self):
        fixture,_,half=await self.original_path()
        await self.assert_denied(await self.record(fixture,[half],ADMISSION))
        self.message,self.job,self.attempt=[uuid.uuid4()for _ in range(3)]
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,revision,raw_text,posted_at,media_state,media_objects)VALUES($1,$2,$3,'100',2,'上半场大0.5',now(),'ready',$4)",self.message,self.source,self.expert,[{'sha256':'b'*64,'mime_type':'image/jpeg','object_key':'isolated-revision.jpg'}])
        await self.pool.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,attempt_id,lease_until)VALUES($1,$2,'running',$3,$4,now()+interval '5 minutes')",self.job,self.message,str(self.job),self.attempt)
        key,_=await self.search_all();await self.resolve(key)
        half,=await self.read(fixture,market_kind='total',period='first_half',line='0.5')
        result=await self.record(fixture,[half],'修订原文上半场大0.5')
        self.assertEqual([],result['rejected']);self.assertEqual(1,len(result['accepted']))

    async def test_duplicate_concurrent_admissions_remain_denied(self):
        fixture,_,half=await self.original_path()
        for result in await asyncio.gather(self.record(fixture,[half],ADMISSION),self.record(fixture,[half],ADMISSION)):
            await self.assert_denied(result)

    async def assert_conflicting_batch_denied(self,reverse):
        fixture,_,half=await self.original_path()
        evidence=[ADMISSION,'原图所选上半场大0.5']
        if reverse:evidence.reverse()
        picks=[{'fixture_id':fixture,'quote_id':half['quote_id'],'evidence_text':text}for text in evidence]
        response=await self.post('record_expert_signals',signals=picks)
        self.assertEqual(200,response.status_code,response.text)
        await self.assert_denied(response.json())
        self.assertEqual(2,len(response.json()['rejected']))

    async def test_reworded_pick_before_admission_in_same_batch_is_not_accepted(self):
        await self.assert_conflicting_batch_denied(True)

    async def test_reworded_pick_after_admission_in_same_batch_is_not_accepted(self):
        await self.assert_conflicting_batch_denied(False)

    async def test_concurrent_rewording_waits_for_committed_denial(self):
        fixture,_,half=await self.original_path()
        entered,release=asyncio.Event(),asyncio.Event()
        original=reviews.tool_success
        async def hold_denial(conn,body,result,*args,**kwargs):
            if result.get('rejected')and not entered.is_set():
                entered.set();await asyncio.wait_for(release.wait(),3)
            return await original(conn,body,result,*args,**kwargs)
        first=second=None
        with patch.object(reviews,'tool_success',side_effect=hold_denial):
            try:
                first=asyncio.create_task(self.record(fixture,[half],ADMISSION))
                await asyncio.wait_for(entered.wait(),3)
                second=asyncio.create_task(self.record(fixture,[half],'原图所选上半场大0.5'))
                await asyncio.sleep(.03)
                self.assertFalse(second.done())
            finally:release.set()
            for task in (first,second):
                if task is not None:await self.assert_denied(await task)
