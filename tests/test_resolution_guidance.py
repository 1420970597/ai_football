"""Ambiguous pairs need source-based resolution, never invented bindings."""
from datetime import timedelta
import os
from unittest.mock import AsyncMock
import unittest
import uuid

from app.review_resolution import resolve_candidates
from app.review_tool_evidence import snapshot_result
from app.routes.reviews import ResolveInput
import test_reviews as support


class ResolutionGuidanceProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_identity_hint_is_bounded_and_never_writes_binding(self):
        conn=AsyncMock()
        body=ResolveInput(review_id=uuid.uuid4(),attempt_id=uuid.uuid4(),search_key='a'*64,
                          team_a='Alpha',team_b='Beta')
        rows=[{'fixture_id':f'leyu:{n}','candidate_data':{'id':f'leyu:{n}',
            'home_team':'Alpha','away_team':'Beta','sport':'football','phase':'scheduled',
            'competition_name':'League','kickoff_at':'2026-11-08T14:00:00+00:00',
            'secret':'not-public'}}for n in range(23)]
        result=await resolve_candidates(conn,body,rows)
        self.assertFalse(result['resolved'])
        self.assertEqual(23,result['candidate_count'])
        self.assertEqual(20,len(result['matching_candidates']))
        self.assertFalse(result['matching_candidates_complete'])
        self.assertTrue(all('secret' not in r for r in result['matching_candidates']))
        conn.fetchval.assert_not_awaited()

    async def test_ambiguity_hint_survives_exact_public_projection_without_private_fields(self):
        candidate=dict(id='leyu:1',home_team='Alpha',away_team='Beta',sport='football',
            competition_name='League',kickoff_at='2026-11-08T14:00:00+00:00',phase='scheduled')
        resolution=dict(resolved=False,reason='ambiguous_fixture',candidate_count=2,
            matching_candidates=[candidate|{'secret':'not-public'}],matching_candidates_complete=True)
        guide=dict(condition='source_pair_found_but_not_unique',
            action='resolve_match_with_source_competition_and_time',
            required_scope=['search_key','team_a','team_b','competition','from_utc','to_utc'],
            does_not_confirm_identity=True)
        result=snapshot_result('mcp_search_matches',dict(search_key='a'*64,items=[],
            search_complete=True,next_cursor=None,total_candidates=2,resolution=resolution,
            next_search_guidance=guide|{'secret':'not-public'}))
        self.assertEqual([candidate],result['resolution']['matching_candidates'])
        self.assertTrue(result['resolution']['matching_candidates_complete'])
        self.assertEqual(guide,result['next_search_guidance'])


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires task-owned isolated database')
class ResolutionGuidanceAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_real_league_and_same_named_eafc_require_actual_source_disambiguation(self):
        real=await self.fixture(1,home_team='水晶宫',away_team='利物浦',
            competition_name='英格兰超级联赛')
        game=await self.fixture(2,home_team='水晶宫',away_team='利物浦',
            competition_name='VS- 英格兰超级联赛 PANDA独家EAFC24',
            kickoff_at=(self.now-timedelta(hours=3)).isoformat())
        response=await self.post('search_matches',team_query='水晶宫',team_a='水晶宫',team_b='利物浦')
        self.assertEqual(200,response.status_code,response.text)
        data=response.json();resolution=data['resolution']
        self.assertEqual('ambiguous_fixture',resolution['reason'])
        self.assertEqual({real,game},{r['id']for r in resolution['matching_candidates']})
        self.assertTrue(resolution['matching_candidates_complete'])
        self.assertEqual('resolve_match_with_source_competition_and_time',data['next_search_guidance']['action'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        result=await self.post('resolve_match',search_key=data['search_key'],team_a='水晶宫',team_b='利物浦',
            competition='英格兰超级联赛',from_utc=self.now.isoformat(),
            to_utc=(self.now+timedelta(hours=4)).isoformat())
        self.assertTrue(result.json()['resolved'],result.text)
        self.assertEqual(real,result.json()['fixture']['id'])
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_same_league_time_and_pair_remain_unresolved_after_hint(self):
        await self.fixture(1,home_team='Alpha',away_team='Beta',competition_name='League')
        await self.fixture(2,home_team='Alpha',away_team='Beta',competition_name='League')
        page=(await self.post('search_matches',team_query='Alpha',team_a='Alpha',team_b='Beta')).json()
        self.assertEqual(2,len(page['resolution']['matching_candidates']))
        result=await self.post('resolve_match',search_key=page['search_key'],team_a='Alpha',team_b='Beta',
            competition='League',from_utc=self.now.isoformat(),to_utc=(self.now+timedelta(hours=4)).isoformat())
        self.assertEqual('ambiguous_fixture',result.json()['reason'])
        self.assertFalse(result.json()['resolved'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
