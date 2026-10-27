"""Order-stage evidence uses isolated account fixtures and mock native HTTP only."""
import hashlib
import json
import unittest
import uuid

import asyncpg
import httpx
from fastapi import HTTPException

import test_account_state as account_fixture
from services.api.app import orders, order_evidence
from services.executor.account_protocol import AccountProtocolClient, NativeSelection, BET_PATH
from services.executor.account_session import AccountScope, AccountSessionManager, RenewalHooks


@unittest.skipUnless(account_fixture.DSN,'requires task-owned temporary Docker database')
class OrderStageEvidenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = account_fixture.AccountStateTests.asyncSetUp
    asyncTearDown = account_fixture.AccountStateTests.asyncTearDown
    recent_session = account_fixture.AccountStateTests.recent_session
    bind = account_fixture.AccountStateTests.bind
    healthy = account_fixture.AccountStateTests.healthy
    signal = account_fixture.AccountStateTests.signal
    enable = account_fixture.AccountStateTests.enable
    reserve = account_fixture.AccountStateTests.reserve
    native = account_fixture.AccountStateTests.native
    executor = account_fixture.AccountStateTests.executor

    async def prepared_order(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        order=await self.reserve(signal)
        work=(await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        manager=AccountSessionManager(AccountScope(**work['scope']),work['config'],RenewalHooks(None,None,None))
        async with AccountProtocolClient(manager.scope,manager.venue_session(),transport=httpx.MockTransport(self.native)) as client:
            prepared=await client.preflight(NativeSelection(**work['selection']),order['stake'],device_imei=work['config']['leyu_app_uuid'])
        return order,work,prepared

    async def authorize(self,order,work,prepared,**updates):
        return await self.worker_api.post(f"/internal/executor/orders/{order['id']}/authorize",json={
            'attempt_id':work['attempt_id'],'prepared_digest':prepared.digest,'payload_json':prepared.payload_json,**updates})

    async def evidence(self,order):
        return await self.pool.fetch("SELECT * FROM v2_quote_evidence WHERE evidence_kind='order_attempt' AND reference_id=$1 ORDER BY evidence->>'stage'",order['id'])

    async def test_authorization_appends_two_whitelisted_stages_and_replay_never_issues_permit(self):
        order,work,prepared=await self.prepared_order()
        payload=prepared.payload
        payload['Authorization']='private-header-sentinel'
        payload['session']={'token':'private-token-sentinel','password':'private-password-sentinel'}
        encoded=json.dumps(payload,sort_keys=True,separators=(',',':'))
        prefix=f"{work['scope']['user_id']}:{work['scope']['account_id']}:{work['scope']['revision']}:"
        digest=hashlib.sha256((prefix+encoded).encode()).hexdigest()
        first=await self.authorize(order,work,prepared,payload_json=encoded,prepared_digest=digest)
        self.assertEqual(200,first.status_code,first.text)
        records=await self.evidence(order)
        self.assertEqual(['preflight_validated','submission_authorized'],[r['evidence']['stage'] for r in records])
        saved=json.dumps([dict(r) for r in records],default=str)
        for secret in ('private-header-sentinel','private-token-sentinel','private-password-sentinel',
                       'own-password','own-business-session',work['config']['leyu_app_uuid'],
                       'deviceImei','payload_json','Authorization','session','token','password'):
            self.assertNotIn(secret,saved)
        for record in records:
            document=record['evidence']
            self.assertEqual('order-stage-v1',document['contract'])
            self.assertFalse(document['submission_confirmed'])
            self.assertEqual(digest,document['prepared_digest'])
            self.assertEqual(work['attempt_id'],document['attempt_id'])
            self.assertEqual('2',document['stake'])
            self.assertEqual('1.95',document['price'])
            self.assertEqual(record['source_updated_at'].isoformat(),document['source_updated_at'])
            actual={k:v for k,v in document.items() if k!='content_sha256'}
            expected=hashlib.sha256(json.dumps(actual,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            self.assertEqual(expected,document['content_sha256'])
        self.assertEqual(str(records[0]['id']),records[1]['evidence']['preflight_evidence_id'])
        replay=await self.authorize(order,work,prepared,payload_json=encoded,prepared_digest=digest)
        self.assertEqual(409,replay.status_code,replay.text)
        self.assertEqual(records,await self.evidence(order))
        self.assertNotIn(BET_PATH,[request.url.path for request in self.native_requests])

    async def test_identical_stage_hash_is_idempotent_and_conflict_preserves_first(self):
        order,work,prepared=await self.prepared_order()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                locked=await conn.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1 FOR UPDATE',order['id'])
                signal=await orders.eligible_signal(conn,order['signal_id'],60)
                first=await order_evidence.append_stage(conn,locked,signal,uuid.UUID(work['attempt_id']),prepared.digest,'preflight_validated')
                second=await order_evidence.append_stage(conn,locked,signal,uuid.UUID(work['attempt_id']),prepared.digest,'preflight_validated')
                self.assertEqual(first,second)
        saved=await self.evidence(order)
        async with self.pool.acquire() as conn:
            with self.assertRaisesRegex(HTTPException,'订单阶段证据冲突'):
                async with conn.transaction():
                    locked=await conn.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1 FOR UPDATE',order['id'])
                    signal=await orders.eligible_signal(conn,order['signal_id'],60)
                    await order_evidence.append_stage(conn,locked,signal,uuid.UUID(work['attempt_id']),'0'*64,'preflight_validated')
        self.assertEqual(saved,await self.evidence(order))

    async def test_existing_conflicting_stage_rejects_authorization_without_mutating_order(self):
        order,work,prepared=await self.prepared_order()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                locked=await conn.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1 FOR UPDATE',order['id'])
                signal=await orders.eligible_signal(conn,order['signal_id'],60)
                await order_evidence.append_stage(conn,locked,signal,uuid.UUID(work['attempt_id']),'0'*64,'preflight_validated')
        first=await self.evidence(order)
        response=await self.authorize(order,work,prepared)
        self.assertEqual(409,response.status_code,response.text)
        self.assertEqual(first,await self.evidence(order))
        row=await self.pool.fetchrow('SELECT state,prepared_digest,submitted_at FROM v2_bet_orders WHERE id=$1',order['id'])
        self.assertEqual(('pending',None,None),tuple(row.values()))

    async def test_second_stage_failure_rolls_back_first_stage_and_send_transition(self):
        order,work,prepared=await self.prepared_order()
        await self.pool.execute("CREATE FUNCTION reject_stage() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.evidence->>'stage'='submission_authorized' THEN RAISE EXCEPTION 'isolated stage fault'; END IF; RETURN NEW; END $$; CREATE TRIGGER reject_stage BEFORE INSERT ON v2_quote_evidence FOR EACH ROW EXECUTE FUNCTION reject_stage()")
        with self.assertRaises(asyncpg.RaiseError):
            await self.authorize(order,work,prepared)
        self.assertEqual([],await self.evidence(order))
        row=await self.pool.fetchrow('SELECT state,prepared_digest,submitted_at,reserved_stake FROM v2_bet_orders WHERE id=$1',order['id'])
        self.assertEqual(('pending',None,None,order['reserved_stake']),tuple(row.values()))

    async def test_invalid_prepared_content_and_stale_quote_never_add_stage(self):
        order,work,prepared=await self.prepared_order()
        self.assertEqual(422,(await self.authorize(order,work,prepared,prepared_digest='0'*64)).status_code)
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '5 minutes'")
        self.assertEqual(409,(await self.authorize(order,work,prepared)).status_code)
        self.assertEqual([],await self.evidence(order))

    async def test_wrong_attempt_and_outside_transaction_cannot_append_evidence(self):
        order,work,prepared=await self.prepared_order()
        self.assertEqual(409,(await self.authorize(order,work,prepared,attempt_id=str(uuid.uuid4()))).status_code)
        async with self.pool.acquire() as conn:
            signal=await orders.eligible_signal(conn,order['signal_id'],60)
            with self.assertRaisesRegex(RuntimeError,'authorization transaction'):
                await order_evidence.append_stage(conn,order,signal,uuid.UUID(work['attempt_id']),prepared.digest,'preflight_validated')
        self.assertEqual([],await self.evidence(order))

    async def test_winner_null_stage_retains_explicit_kind_without_zero_filling(self):
        order,work,prepared=await self.prepared_order()
        snapshot=order['quote_snapshot']|{'kind':'winner','line':None,'chpid':'1','selection_code':'1'}
        await self.pool.execute("UPDATE v2_expert_signals SET market_kind='winner',market_id='1',market_name='独赢',line=NULL,selection='home' WHERE id=$1",order['signal_id'])
        await self.pool.execute("UPDATE v2_current_quotes SET market_kind='winner',market_id='1',market_name='独赢',line=NULL,selection='home'")
        await self.pool.execute('UPDATE v2_bet_orders SET quote_snapshot=$2 WHERE id=$1',order['id'],snapshot)
        payload=prepared.payload
        payload['seriesOrders'][0]['orderDetailList'][0].update(marketValue='',playOptions='1')
        encoded=json.dumps(payload,sort_keys=True,separators=(',',':'))
        prefix=f"{work['scope']['user_id']}:{work['scope']['account_id']}:{work['scope']['revision']}:"
        digest=hashlib.sha256((prefix+encoded).encode()).hexdigest()
        response=await self.authorize(order,work,prepared,payload_json=encoded,prepared_digest=digest)
        self.assertEqual(200,response.status_code,response.text)
        records=await self.evidence(order)
        self.assertEqual(2,len(records))
        self.assertTrue(all(row['line'] is None and row['evidence']['line'] is None and row['evidence']['market_kind']=='winner' for row in records))

    async def test_unknown_send_preserves_two_stages_and_exposure_without_retry(self):
        await self.healthy()
        signal,expert,_,_=await self.signal()
        await self.enable([expert])
        order=await self.reserve(signal)
        work=(await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()
        self.native_submission='unknown'
        await self.executor().order_job(work)
        self.assertEqual(1,sum(request.url.path==BET_PATH for request in self.native_requests))
        self.assertEqual(2,len(await self.evidence(order)))
        stored=await self.pool.fetchrow('SELECT state,reserved_stake FROM v2_bet_orders WHERE id=$1',order['id'])
        self.assertEqual(('unknown',order['stake']),tuple(stored.values()))
        self.assertEqual('idle',(await self.worker_api.post('/internal/executor/claim?live_capable=true')).json()['kind'])
        self.assertEqual(1,sum(request.url.path==BET_PATH for request in self.native_requests))


if __name__=='__main__':
    unittest.main()
