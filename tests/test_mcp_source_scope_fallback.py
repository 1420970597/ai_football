import json
import unittest
from unittest.mock import AsyncMock,patch
import test_mcp_transport as support

class SourceScopeFallbackTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.MCPTransportTests.asyncSetUp
    asyncTearDown=support.MCPTransportTests.asyncTearDown

    def payload(self,**updates):
        return dict(review_id='job',attempt_id='attempt',team_query='source A',team_a='source A',
            team_b='source B',competition_query='source league',sport='football',phase='all',
            from_utc='2026-11-14T00:00:00Z',to_utc='2026-11-17T00:00:00Z',cursor=None,**updates)

    def response(self,action=None,**updates):
        return dict(search_key='a'*64,search_complete=True,next_cursor=None,items=[],total_candidates=0,
            resolution={'resolved':False,'candidate_count':0},
            **({'next_search_guidance':{'action':action}}if action else {}))|updates

    async def test_native_guidance_runs_two_bounded_scopes_without_loosening_identity(self):
        calls=AsyncMock(side_effect=[self.response('search_source_team_without_competition_filter'),
            self.response('search_complete_league_candidates'),self.response()])
        with patch.object(self.server,'_post',calls):
            value=await self.server._search_source_scopes(self.payload())
        self.assertEqual(3,calls.await_count)
        bodies=[c.args[1]for c in calls.await_args_list]
        self.assertIsNone(bodies[1]['competition_query'])
        self.assertEqual('',bodies[2]['team_query'])
        self.assertEqual('source league',bodies[2]['competition_query'])
        for body in bodies:
            for key in('review_id','attempt_id','team_a','team_b','sport','from_utc','to_utc'):
                self.assertEqual(self.payload()[key],body[key])
        self.assertFalse(value['resolution']['resolved'])
        self.assertFalse(value['bounded_source_search']['retry_same_scope'])
        self.assertEqual(2,len(value['prior_source_search_scopes']))

    async def test_incomplete_fallback_keeps_cursor_and_exact_scope_without_completion_claim(self):
        calls=AsyncMock(side_effect=[self.response('search_complete_league_candidates'),
            self.response(next_cursor='native-next',search_complete=False)])
        with patch.object(self.server,'_post',calls):
            value=await self.server._search_source_scopes(self.payload())
        self.assertEqual('native-next',value['next_cursor'])
        self.assertEqual('',value['source_search_scope']['team_query'])
        self.assertNotIn('bounded_source_search',value)

    async def test_unbounded_missing_ambiguous_resolved_and_unknown_guidance_do_not_expand(self):
        for updates,result in [({'from_utc':None},self.response('search_complete_league_candidates')),
            ({'to_utc':'2026-12-17T00:00:00Z'},self.response('search_complete_league_candidates')),
            ({'competition_query':None},self.response('search_complete_league_candidates')),
            ({},self.response('resolve_match_with_source_competition_and_time')),
            ({},self.response('search_complete_league_candidates',search_complete=False)),
            ({},self.response('search_complete_league_candidates',resolution={'resolved':True}))]:
            calls=AsyncMock(return_value=result)
            with patch.object(self.server,'_post',calls):
                value=await self.server._search_source_scopes(self.payload()|updates)
            self.assertEqual(1,calls.await_count)
            self.assertNotIn('bounded_source_search',value)

    async def test_batch_continues_fallback_cursor_in_its_actual_scope(self):
        item=self.server.SourceMatchIntent(team_a='source A',team_b='source B',
            competition_query='source league',sport='football',
            from_utc='2026-11-14T00:00:00Z',to_utc='2026-11-17T00:00:00Z',
            market_kind='total',market_query='大0.5',period=None,source_periods=[],evidence_text='source A v source B 大0.5')
        calls=AsyncMock(side_effect=[self.response('search_complete_league_candidates'),
            self.response(search_complete=False,next_cursor='native-next'),self.response()])
        with patch.object(self.server,'_post',calls):
            value=await self.server._review_batch_item('job','attempt',item)
        self.assertEqual(3,calls.await_count)
        last=calls.await_args_list[-1].args[1]
        self.assertEqual('',last['team_query'])
        self.assertEqual('native-next',last['cursor'])
        self.assertEqual('source league',last['competition_query'])
        self.assertEqual('source_period_unknown',value['source_intent_recording']['reason'])

    async def test_fallback_failure_preserves_completed_native_receipt_without_retry(self):
        first=self.response('search_complete_league_candidates')
        calls=AsyncMock(side_effect=[first,self.server.ToolError('upstream unavailable')])
        with patch.object(self.server,'_post',calls):
            with self.assertRaises(self.server.ToolError)as caught:
                await self.server._search_source_scopes(self.payload())
        self.assertEqual(first,json.loads(str(caught.exception))['completed_receipt'])
        self.assertEqual(2,calls.await_count)
