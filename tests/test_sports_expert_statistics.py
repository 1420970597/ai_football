"""Sport scopes over synthetic evidence, never proof of provider settlement support."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from app.expert_profitability import profitability_rows
from app.expert_statistics import StatisticsScope, signal_scope
from app.expert_statistics_reset import apply_reset, preview_reset, revoke_reset
from app.routes import public
import test_result_ledger as support


class SportScopeContractTests(unittest.TestCase):
    def test_internal_default_preserves_combined_profitability_and_rejects_unknown_sports(self):
        now=datetime.now(timezone.utc)
        scope=StatisticsScope(now-timedelta(days=30),now)
        self.assertEqual('all',scope.sport)
        self.assertEqual('all',signal_scope(scope)[1][-1])
        self.assertEqual('all',scope.response()['filters']['sport'])
        with self.assertRaisesRegex(Exception,'422'):
            signal_scope(StatisticsScope(scope.start,scope.end,sport='tennis'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires task-owned isolated PostgreSQL')
class SportStatisticsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ResultLedgerTests.asyncSetUp
    asyncTearDown=support.ResultLedgerTests.asyncTearDown

    async def evidence(self,sport,values,*,source=None):
        """Known outcomes test aggregation only; no settlement algorithm is invoked."""
        fixture='isolated-sport-'+uuid.uuid4().hex
        await self.pool.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) "
                                "VALUES($1,$1,$2,'隔离联赛','隔离主队','隔离客队',$3,'finished')",fixture,sport,self.now)
        await self.pool.execute("INSERT INTO v2_fixture_results(fixture_id,version,provider_reference,content_hash,status,home_score,away_score,source_updated_at,evidence) "
                                "VALUES($1,1,'synthetic-statistics-only','synthetic-statistics-only','confirmed',3,1,$2,$3)",
                                fixture,self.now,{'synthetic':True,'provider_settlement_proof':False})
        ids=[]
        for outcome,net,stake in values:
            message=uuid.uuid4()
            await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) "
                                    "VALUES($1,$2,$3,$4,'隔离推荐',now())",message,source or self.source,self.expert,str(message))
            identity=uuid.uuid4();ids.append(identity)
            await self.pool.execute("INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,market_kind,period,line,selection,recommendation_price,quote_time,confidence,score_basis,outcome,unit_net,effective_stake,settled_at,result_version,settlement_reason) "
                                    "VALUES($1,$2,$3,$4,'scheduled','total','大小','total','full_time',2.5,'over',2,$5,.8,'full_period',$6,$7,$8,$9,1,'synthetic-statistics-only')",
                                    identity,message,self.expert,fixture,self.now,outcome,Decimal(net)if net is not None else None,Decimal(stake),self.now if outcome!='pending'else None)
            await self.pool.execute("INSERT INTO v2_quote_evidence(id,evidence_kind,reference_id,fixture_id,market_id,market_name,period,line,selection,price,source_updated_at,evidence,score_baseline) "
                                    "SELECT $1,'expert_signal',id,fixture_id,market_id,market_name,period,line,selection,recommendation_price,quote_time,jsonb_build_object('score_basis',score_basis),score_baseline FROM v2_expert_signals WHERE id=$2",uuid.uuid4(),identity)
            if outcome!='pending':
                await self.pool.execute("INSERT INTO v2_signal_settlements(signal_id,fixture_id,result_version,algorithm_version,outcome,unit_net,effective_stake,reason,calculation) "
                                        "SELECT id,fixture_id,1,'synthetic-statistics-only',outcome,unit_net,effective_stake,settlement_reason,'{}'::jsonb FROM v2_expert_signals WHERE id=$1",identity)
        return ids

    async def listing(self,sport=None,**params):
        response=await self.client.get('/api/v1/public/experts',params={**({'sport':sport}if sport else{}),**params})
        self.assertEqual(200,response.status_code,response.text)
        return response.json()

    async def test_same_expert_football_basketball_have_independent_accuracy_and_returns(self):
        await self.evidence('football',[('win','1','1'),('half_loss','-.5','.5'),('push','0','0'),('pending',None,'0')])
        await self.evidence('basketball',[('half_win','.5','.5'),('loss','-1','1'),('void','0','0')])
        football=(await self.listing('football'))['items'][0]
        basketball=(await self.listing('basketball'))['items'][0]
        combined=(await self.listing())['items'][0]
        self.assertEqual((4,3,2,.5),(football['total_signals'],football['settled_signals'],football['effective_settled_signals'],football['unit_net']))
        self.assertAlmostEqual(2/3,football['accuracy'])
        self.assertEqual((3,3,2,-.5),(basketball['total_signals'],basketball['settled_signals'],basketball['effective_settled_signals'],basketball['unit_net']))
        self.assertAlmostEqual(1/3,basketball['accuracy'])
        self.assertEqual((7,6,0,.5),(combined['total_signals'],combined['settled_signals'],combined['unit_net'],combined['accuracy']))
        eligibility=(await profitability_rows(self.pool,expert_id=self.expert))[0]
        self.assertEqual((4,Decimal('0')),(eligibility['effective_settled_signals'],eligibility['unit_net']))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_detail_trend_filters_and_source_options_share_exact_sport_scope(self):
        other=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name)VALUES($1,'basketball-source','篮球来源')",other)
        await self.evidence('football',[('win','1','1')])
        await self.evidence('basketball',[('loss','-1','1')],source=other)
        params={'sport':'basketball','source_id':str(other),'phase':'scheduled','market_kind':'total','days':1}
        listing=await self.listing(**params)
        detail=(await self.client.get(f'/api/v1/public/experts/{self.expert}',params=params)).json()
        trend=(await self.client.get(f'/api/v1/public/experts/{self.expert}/trend',params=params)).json()
        self.assertEqual(listing['items'][0],detail['expert'])
        self.assertEqual(detail['expert'],trend['summary'])
        self.assertEqual('basketball',detail['filters']['sport'])
        self.assertEqual((1,-1),(trend['items'][0]['cumulative_settled'],trend['items'][0]['cumulative_unit_net']))
        self.assertEqual([{'id':str(other),'display_name':'篮球来源'}],listing['source_options'])
        for params in ({'sport':'basketball','phase':'live'},{'sport':'football','source_id':str(other)},
                       {'sport':'basketball','market_kind':'handicap'}):
            self.assertIsNone((await self.listing(**params))['items'][0]['accuracy'])

    async def test_bad_evidence_neutral_and_pending_do_not_become_basketball_losses(self):
        signals=await self.evidence('basketball',[('win','1','1'),('push','0','0'),('void','0','0'),('pending',None,'0')])
        await self.pool.execute('DELETE FROM v2_quote_evidence WHERE reference_id=$1',signals[0])
        value=(await self.listing('basketball'))['items'][0]
        self.assertEqual((4,1,2,1),(value['total_signals'],value['excluded_signals'],value['settled_signals'],value['pending']))
        self.assertIsNone(value['accuracy'])
        self.assertEqual(0,value['unit_net'])
        self.assertIsNone((await self.listing('football'))['items'][0]['accuracy'])

    async def test_existing_reset_excludes_both_sports_and_revoke_restores_each_scope(self):
        await self.evidence('football',[('win','1','1')])
        await self.evidence('basketball',[('loss','-1','1')])
        plan=await preview_reset(self.pool)
        self.assertEqual(2,len(plan['signals']))
        reset=await apply_reset(self.pool,idempotency_key='isolated-sports-reset',expected_fingerprint=plan['fingerprint'],
                                as_of=datetime.fromisoformat(plan['as_of']),reason='synthetic scope regression')
        for sport in ('football','basketball'):
            row=(await self.listing(sport))['items'][0]
            self.assertIsNotNone(row['statistics_reset_at'])
            self.assertEqual(0,row['total_signals'])
            self.assertIsNone(row['accuracy'])
        await revoke_reset(self.pool,reset_id=uuid.UUID(reset['reset_id']),reason='synthetic restore')
        self.assertEqual(1,(await self.listing('football'))['items'][0]['accuracy'])
        self.assertEqual(0,(await self.listing('basketball'))['items'][0]['accuracy'])

    async def test_sport_signal_pagination_is_scoped_and_membership_stays_enforced(self):
        await self.evidence('football',[('pending',None,'0')]*2)
        await self.evidence('basketball',[('pending',None,'0')]*2)
        with patch.object(public,'_has_entitlement',AsyncMock(return_value=True)),patch.object(public,'get_identity',AsyncMock(return_value={'role':'user'})):
            first=await self.client.get('/api/v1/public/signals',params={'sport':'basketball','page_size':1})
            self.assertEqual(200,first.status_code,first.text)
            body=first.json();self.assertEqual(['basketball'],[r['sport']for r in body['items']])
            self.assertIsNotNone(body['next_cursor'])
            second=await self.client.get('/api/v1/public/signals',params={'sport':'basketball','page_size':1,'cursor':body['next_cursor']})
            self.assertEqual(1,len(second.json()['items']))
            self.assertNotEqual(body['items'][0]['id'],second.json()['items'][0]['id'])
            wrong=await self.client.get('/api/v1/public/signals',params={'sport':'football','cursor':body['next_cursor']})
            self.assertEqual(400,wrong.status_code)
            legacy=(await self.client.get('/api/v1/public/signals',params={'page_size':1})).json()
            self.assertEqual(200,(await self.client.get('/api/v1/public/signals',params={'cursor':legacy['next_cursor']})).status_code)
        guest=await self.client.get('/api/v1/public/signals',params={'sport':'basketball'})
        self.assertTrue(guest.json()['limited'])
        self.assertTrue(all('raw_text'not in row and 'confidence'not in row for row in guest.json()['items']))
        for endpoint in ('/experts',f'/experts/{self.expert}',f'/experts/{self.expert}/trend','/signals'):
            self.assertEqual(422,(await self.client.get('/api/v1/public'+endpoint,params={'sport':'tennis'})).status_code)
