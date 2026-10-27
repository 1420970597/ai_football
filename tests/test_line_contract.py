"""Explicit no-line winner and numeric zero-line contracts; no upstream calls."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import unittest
import uuid

import httpx
from pydantic import ValidationError

from app.orders import prepared_line_matches
from app.routes.internal import FixtureQuote
from app.settlement import settle
from infra.quality.formats import NO_LINE, QualityError, normalize_item
from services.collector.leyu_protocol import LeyuError, parse_quotes
from services.executor.account_protocol import AccountProtocolClient, NativeSelection, AMOUNT_PATH, LATEST_MARKET_PATH, LIMITS_PATH
from services.executor.account_session import AccountScope, VenueSession


def quote(**updates):
    return {'market_id':'1', 'market_name':'独赢', 'kind':'winner', 'line':None,
            'selection':'home', 'price':'1.95', 'received_at':datetime.now(timezone.utc).isoformat(), **updates}


class LineContractTests(unittest.TestCase):
    def test_winner_null_and_explicit_legacy_zero_are_one_semantic_value(self):
        for value in (None, 0, '0.000'):
            self.assertIsNone(FixtureQuote.model_validate(quote(line=value)).line)
        for value in ('0.25', 'NaN', 'Infinity', True, ''):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                FixtureQuote.model_validate(quote(line=value))
        missing = quote()
        del missing['line']
        with self.assertRaises(ValidationError):
            FixtureQuote.model_validate(missing)

    def test_asian_line_zero_is_valid_but_absent_nonfinite_out_of_range_rejected(self):
        for kind in ('total', 'handicap', 'unsupported'):
            self.assertEqual(Decimal(0), FixtureQuote.model_validate(quote(kind=kind,line=0)).line)
            for value in (None, '', 'NaN', 'Infinity', '-Infinity', True, '1000.25'):
                with self.subTest(kind=kind,value=value), self.assertRaises(ValidationError):
                    FixtureQuote.model_validate(quote(kind=kind,line=value))

    def test_protocol_keeps_null_and_explicit_zero_separate_from_missing_asian_line(self):
        def payload(kind, value, present=True):
            line={'hid':'market', 'hs':0, 'ol':[{'ot':'1','ov':195000,'oid':'option','os':1}]}
            if present:
                line['hv']=value
            return {'hps':[{'chpid':kind,'hl':line}]}
        for value in (None, '', '0'):
            parsed=parse_quotes(payload('1',value),'leyu:1')
            self.assertEqual(1,len(parsed))
            self.assertIsNone(parsed[0]['line'])
        self.assertEqual([],parse_quotes(payload('1','0.25'),'leyu:1'))
        self.assertEqual([],parse_quotes(payload('4',None,False),'leyu:1'))
        self.assertEqual('0',parse_quotes(payload('4','0'),'leyu:1')[0]['line'])

    def test_settlement_winner_null_equals_legacy_zero_and_missing_asian_is_pending(self):
        result={'status':'confirmed','home':1,'away':0}
        base={'kind':'winner','selection':'home','line':None,'phase':'scheduled','price':'1.95'}
        for value in (None, '0'):
            actual=settle({**base,'line':value},result)
            self.assertEqual(('win',Decimal('0.95')),(actual.outcome,actual.unit_net))
        for value in (None, '', True, 'NaN'):
            actual=settle({**base,'kind':'handicap','line':value},result)
            self.assertEqual('pending',actual.outcome)
            self.assertIsNone(actual.unit_net)
        missing={**base,'kind':'handicap'}
        del missing['line']
        self.assertEqual('pending',settle(missing,result).outcome)
        self.assertEqual('win',settle({**base,'kind':'handicap','line':'0'},result).outcome)
        for value in ('0.25',True,'NaN'):
            self.assertEqual('pending',settle({**base,'line':value},result).outcome)

    def test_order_prepared_value_requires_numeric_line_for_asian_market(self):
        for raw in (None,'','0',0):
            for saved in (None,'0',0):
                self.assertTrue(prepared_line_matches(raw,saved,'winner'))
        for raw,saved,kind in ((None,None,'handicap'),('',0,'total'),('bad',None,'winner'),
                               ('0.5',None,'winner'),('0','', 'winner'),(True,0,'handicap')):
            self.assertFalse(prepared_line_matches(raw,saved,kind))
        self.assertTrue(prepared_line_matches('0','0','handicap'))
        self.assertTrue(prepared_line_matches('-0.5/1','-0.75','handicap'))

    def test_native_selection_winner_null_legacy_zero_and_numeric_required(self):
        args={'match_id':'1','market_instance_id':'m','play_id':'1','selection_id':'o',
              'selection_code':'1','sport_id':'1','chpid':'1','kind':'winner','period':'full_time',
              'line':None,'price':'1.95','phase':'live'}
        native=NativeSelection(**args)
        self.assertIsNone(native.line)
        self.assertIsNone(replace(native,line=Decimal(0)).line)
        for changes in ({'line':'0.25'},{'line':True},{'kind':'handicap','line':None}):
            with self.subTest(changes=changes),self.assertRaises(LeyuError):
                NativeSelection(**(args|changes))
        self.assertEqual(Decimal(0),NativeSelection(**(args|{'kind':'handicap','line':'0'})).line)

    def test_quality_explicit_no_line_is_assessable_and_missing_key_is_missing(self):
        item={'fixture_id':'f','market_id':'1','market_kind':'winner','period':'full_time',
              'line':None,'selection':'home','phase':'scheduled'}
        parsed,missing,invalid=normalize_item(item,expected=True)
        self.assertEqual((NO_LINE,[],[]),(parsed['line'],missing,invalid))
        self.assertEqual(parsed,normalize_item(item|{'line':'0'},expected=True)[0])
        del item['line']
        with self.assertRaises(QualityError):
            normalize_item(item,expected=True)
        with self.assertRaises(QualityError):
            normalize_item(item|{'line':'0.25'},expected=True)


class NativeNoLinePreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_winner_provider_empty_or_zero_is_compatible_but_asian_missing_is_rejected(self):
        owner=AccountScope(str(uuid.uuid4()),str(uuid.uuid4()),1)
        session=VenueSession(owner,'https://isolated.invalid','isolated-session','https://ui.invalid')
        args={'match_id':'f','market_instance_id':'m','play_id':'1','selection_id':'o','selection_code':'1',
              'sport_id':'1','chpid':'1','kind':'winner','period':'full_time','line':None,'price':'1.95','phase':'live'}
        for kind,value in [('winner',None),('winner',''),('winner','0'),('handicap',None)]:
            seen=[]
            def handler(request):
                seen.append(request.url.path)
                if request.url.path==AMOUNT_PATH:
                    data={'amount':'100'}
                elif request.url.path==LIMITS_PATH:
                    data=[{'code':0,'playOptionsId':'o','minBet':'1','orderMaxPay':'10'}]
                elif request.url.path==LATEST_MARKET_PATH:
                    data=[{'id':'m','matchInfoId':'f','playId':'1','matchStatus':1,'matchOver':0,
                           'matchHandicapStatus':0,'status':0,'marketValue':value,
                           'marketOddsList':[{'id':'o','oddsType':'1','oddsStatus':1,'oddsValue':'195000'}]}]
                else:
                    raise AssertionError('only read-only preflight endpoints are permitted')
                return httpx.Response(200,json={'code':'0','data':data})
            async with AccountProtocolClient(owner,session,transport=httpx.MockTransport(handler)) as client:
                selected=NativeSelection(**(args|{'kind':kind,'line':None if kind=='winner' else '0'}))
                if kind=='winner':
                    prepared=await client.preflight(selected,Decimal(1))
                    payload=json.loads(prepared.payload_json)
                    self.assertEqual('' if value is None else value,payload['seriesOrders'][0]['orderDetailList'][0]['marketValue'])
                else:
                    with self.assertRaisesRegex(LeyuError,'selected_native_quote_changed'):
                        await client.preflight(selected,Decimal(1))
            self.assertEqual(3,len(seen))


if __name__=='__main__':
    unittest.main()
