from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import unittest

import httpx

from app.settlement import settle
from app.team_totals import team_total_terms
from app.score_rules import settlement_rule
from services.collector.leyu import LeyuClient, DEFAULT_MATCH_ODDS_PATH
from services.collector.leyu_protocol import parse_quotes
from tests.test_leyu_protocol import market_row


def native_play(mid='5715236', market='10'):
    # Minimal ordinary-football response shape verified 2026-11-06. Native
    # identifiers are synthetic; no account, cookie or order data is included.
    return {'mid': mid, 'chpid': market, 'hpid': market, 'hpn': '主队进球大小' if market == '10' else '客队进球大小',
            'ctsp': '1793959127470', 'hpt': 5,
            'hl': [{'hid': 'team-main', 'hv': '2.5', 'hs': 0, 'hmt': 0, 'ol': [
                {'oid': 'team-over', 'ot': 'Over', 'on': '2.5', 'os': 1, 'ov': 338000},
                {'oid': 'team-under', 'ot': 'Under', 'on': '2.5', 'os': 1, 'ov': 127000}]},
                {'hid': 'team-extra', 'hv': '2.5/3', 'hs': 0, 'hmt': 0, 'ol': [
                {'oid': 'extra-over', 'ot': 'Over', 'os': 1, 'ov': 470000}]}]}


class TeamTotalProtocolTests(unittest.TestCase):
    def test_verified_same_match_supplement_is_not_lost_or_merged_between_teams(self):
        row = market_row('5715236')
        response = {'data': [row], 'playData': [native_play(), native_play(market='11'), native_play('other')]}
        prepared, = LeyuClient._single_match_rows(response, '5715236')
        quotes = parse_quotes(prepared, 'leyu:5715236')
        totals = [q for q in quotes if q['kind'] == 'team_total']
        self.assertEqual(6, len(totals))
        self.assertEqual({'10', '11'}, {q['market_id'] for q in totals})
        self.assertEqual({'2.5', '2.75'}, {q['line'] for q in totals})
        self.assertTrue(all(q['status'] == 'active' and q['score_basis'] == 'unknown' for q in totals))
        self.assertTrue(all(q['is_main'] is False for q in totals))
        self.assertEqual({'home','away'}, {team_total_terms(q, 'leyu:5715236')['team_side'] for q in totals})
        self.assertNotIn('_verified_team_play_data', row)

    def test_unknown_sport_cross_match_and_mismatched_native_ids_are_not_promoted(self):
        for sport in ('2', '', '999', None):
            prepared, = LeyuClient._single_match_rows({'data': [market_row('5715236',csid=sport)],
                'playData': [native_play()]}, '5715236')
            self.assertFalse(any(q['kind'] == 'team_total' for q in parse_quotes(prepared,'leyu:5715236')))
        bad=native_play();bad['chpid']='11'
        prepared, = LeyuClient._single_match_rows({'data': [market_row('5715236')],
            'playData': [bad,native_play('other'),native_play(market='87')]}, '5715236')
        self.assertEqual([], prepared['_verified_team_play_data'])

    def test_native_status_source_time_and_direction_remain_authoritative(self):
        for status, expected in ((0,'active'),(1,'suspended'),(2,'closed')):
            play=native_play();play.pop('ctsp');play['hl'][0]['hs']=status
            row=market_row('5715236',_verified_team_play_data=[play])
            quote=next(q for q in parse_quotes(row,'leyu:5715236')if q['market_id']=='10')
            self.assertEqual(expected,quote['status']);self.assertIsNone(quote['source_updated_at'])
        play=native_play();play['hl'][0]['ol'][0]['ot']='1'
        row=market_row('5715236',_verified_team_play_data=[play])
        self.assertFalse(any(q['selection']=='home'for q in parse_quotes(row,'leyu:5715236')if q['market_id']=='10'))

    def test_team_identity_requires_provider_native_id_and_period(self):
        base={'market_kind':'team_total','market_id':'10','play_id':'10','period':'full_time'}
        self.assertEqual({'team_side':'home'},team_total_terms(base,'leyu:5715236'))
        for updates, fixture in (({'play_id':'11'},'leyu:5715236'),({'period':'first_half'},'leyu:5715236'),
                                ({'market_id':'87'},'leyu:5715236'),({},'other:5715236')):
            self.assertEqual({},team_total_terms(base|updates,fixture))


class TeamTotalClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_existing_detail_request_supplies_verified_quotes_and_observation(self):
        requests=[]
        row=market_row('5715236');now=datetime.now(timezone.utc)
        async def handler(request):
            requests.append(request)
            self.assertEqual(DEFAULT_MATCH_ODDS_PATH,request.url.path)
            return httpx.Response(200,json={'code':'0','data':{'data':[row],'playData':[native_play()]}})
        client=LeyuClient('https://upstream.invalid',request_id='isolated',transport=httpx.MockTransport(handler))
        fixture={'id':'leyu:5715236','source_match_id':'5715236','source_id':'leyu:5715236'}
        result=await client.fetch_match('5715236',fixture=fixture)
        self.assertEqual(1,len(requests))
        self.assertEqual(3,sum(q['kind']=='team_total'for q in result['markets']))
        self.assertEqual(result['markets'],result['_current_observation']['quotes'])
        self.assertEqual(fixture,{'id':'leyu:5715236','source_match_id':'5715236','source_id':'leyu:5715236'})


class TeamTotalSettlementTests(unittest.TestCase):
    def signal(self,**changes):
        return dict(kind='team_total',team_side='home',selection='over',line='1.75',price='1.9',
                    period='full_time',phase='scheduled',**changes)

    def test_team_goals_not_combined_score_and_quarter_outcomes(self):
        result={'status':'confirmed','home':2,'away':1}
        for side,selection,line,outcome,net in (
                ('home','over','1.75','half_win','.45'),('home','under','1.75','half_loss','-.5'),
                ('away','over','1.5','loss','-1'),('away','under','1.5','win','.9'),
                ('home','over','2','push','0')):
            s=self.signal();s.update(team_side=side,selection=selection,line=line)
            value=settle(s,result)
            self.assertEqual((outcome,Decimal(net)),(value.outcome,value.unit_net))

    def test_unknown_live_rule_stays_pending_unless_both_meanings_agree(self):
        s=self.signal();s.update(phase='live',score_basis='unknown',score_baseline={'home':1,'away':0})
        result={'status':'confirmed','home':2,'away':1}
        self.assertEqual('pending',settle(s,result).outcome)
        s['line']='.5';self.assertEqual('win',settle(s,result).outcome)
        self.assertTrue(settle(s,result).reason.endswith(':score_basis_outcome_equivalent'))
        self.assertIsNone(settlement_rule({'id':'leyu:5715236','sport':'football'},
            {'phase':'live','market_kind':'team_total','market_id':'10','period':'full_time','score_basis':'unknown'}))
        for side in (None,'both','draw'):
            bad=self.signal();bad['team_side']=side
            self.assertEqual('pending',settle(bad,result).outcome)
