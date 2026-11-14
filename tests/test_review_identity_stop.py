import unittest
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from app import worker
from app.review_identity_stop import repeated_identity_failure
from app.routes.reviews import ResolveInput
import uuid
import test_reviews as support

class IdentityStopTests(unittest.IsolatedAsyncioTestCase):
    def test_source_phase_contract_keeps_unknown_and_validates_explicit_phase(self):
        args=dict(review_id=uuid.uuid4(),attempt_id=uuid.uuid4(),search_key='k'*64,team_a='Alpha',team_b='Beta')
        self.assertIsNone(ResolveInput(**args).source_phase)
        self.assertEqual('live',ResolveInput(**args,source_phase='live').source_phase)
        with self.assertRaises(ValueError):ResolveInput(**args,source_phase='guess')

    def issues(self):
        return dict(unfinished_search_count=0,unfinished_searches=[],unbound_mention_count=1,
            unbound_mentions=[{'fixture_id':'leyu:1','candidate':{'search_key':'key','search_complete':True}}],
            detail_limit=100)

    async def test_repeated_current_attempt_failure_stops_without_approving_identity(self):
        conn=AsyncMock();conn.fetch.return_value=[{'search_key':'key'}];issues=self.issues()
        self.assertTrue(await repeated_identity_failure(conn,'job','attempt',issues))
        self.assertEqual(1,issues['unbound_mention_count'])
        self.assertNotIn('completion_verified',issues)
        self.assertEqual(('job','attempt'),conn.fetch.call_args.args[1:])
        self.assertIn('HAVING count(*)>=2',conn.fetch.call_args.args[0])

    async def test_single_failure_unread_scope_other_search_or_other_gap_cannot_stop(self):
        for change in({'unfinished_search_count':1},{'source_total_review_gaps':[{}]},
                       {'unbound_mention_count':2}):
            conn=AsyncMock();conn.fetch.return_value=[{'search_key':'key'}]
            self.assertFalse(await repeated_identity_failure(conn,'job','attempt',self.issues()|change))
            conn.fetch.assert_not_awaited()
        for rows in([],[{'search_key':'other'}]):
            conn=AsyncMock();conn.fetch.return_value=rows
            self.assertFalse(await repeated_identity_failure(conn,'job','attempt',self.issues()))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class IdentityStopIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_repeated_failed_tool_identity_ends_unverified_without_extra_model_turn(self):
        await self.fixture(id='leyu:1',home_team='Alpha1',away_team='Beta1')
        r=await self.post('search_matches',team_query='Alpha')
        key=r.json()['search_key']
        for _ in range(2):
            r=await self.post('resolve_match',search_key=key,team_a='Unverified',team_b='Beta1')
            self.assertFalse(r.json()['resolved'])
        text='候选 leyu:1 的来源身份不能核验，保留未解决。'
        job={'id':self.job,'attempt_id':self.attempt,'message':{},'review_mode':'real_time'}
        with patch.object(worker,'review_message',AsyncMock(return_value=(text,{'content':text})))as model:
            _,summary=await worker._review_until_verified(self.pool,SimpleNamespace(),job)
        self.assertEqual(1,model.await_count)
        self.assertFalse(summary['completion_verified'])
        self.assertEqual('repeated_identity_unresolved',summary['completion_terminal'])
        self.assertEqual(1,summary['completion_issues']['unbound_mention_count'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_auto_search_and_explicit_refusal_count_for_same_pair_only(self):
        await self.fixture(id='leyu:1',home_team='Alpha1',away_team='Beta1')
        r=await self.post('search_matches',team_query='Alpha',team_a='Unverified',team_b='Beta1')
        key=r.json()['search_key']
        self.assertFalse(r.json()['resolution']['resolved'])
        issues=IdentityStopTests().issues()
        issues['unbound_mentions'][0]['candidate']['search_key']=key
        async with self.pool.acquire()as conn:
            self.assertFalse(await repeated_identity_failure(conn,self.job,self.attempt,issues))
        await self.post('resolve_match',search_key=key,team_a='Other',team_b='Beta1')
        async with self.pool.acquire()as conn:
            self.assertFalse(await repeated_identity_failure(conn,self.job,self.attempt,issues))
        await self.post('resolve_match',search_key=key,team_a='Unverified',team_b='Beta1')
        async with self.pool.acquire()as conn:
            self.assertTrue(await repeated_identity_failure(conn,self.job,self.attempt,issues))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_complete_empty_pair_and_league_are_terminal_unresolved_not_verified(self):
        issues=dict(unfinished_search_count=0,unfinished_searches=[],unbound_mention_count=0,
            unbound_mentions=[],detail_limit=100,text_intent_unverified=True)
        scope=dict(team_a='Unverified',team_b='Beta1',competition_query='Source League',sport='football',
            from_utc='2026-11-14T00:00:00Z',to_utc='2026-11-17T00:00:00Z')
        await self.post('search_matches',team_query='Unverified',**scope)
        async with self.pool.acquire()as conn:
            self.assertFalse(await repeated_identity_failure(conn,self.job,self.attempt,issues))
        await self.post('search_matches',team_query='',**scope)
        await self.pool.execute("UPDATE v2_analysis_traces SET payload=jsonb_set(payload,'{parameters,team_b}','\"Other\"'::jsonb) WHERE job_id=$1 AND trace_kind='mcp_search_matches' AND payload->'parameters'->>'team_query'=''",self.job)
        async with self.pool.acquire()as conn:
            self.assertFalse(await repeated_identity_failure(conn,self.job,self.attempt,issues))
        await self.pool.execute("UPDATE v2_analysis_traces SET payload=jsonb_set(payload,'{parameters,team_b}','\"Beta1\"'::jsonb) WHERE job_id=$1 AND trace_kind='mcp_search_matches' AND payload->'parameters'->>'team_query'=''",self.job)
        async with self.pool.acquire()as conn:
            self.assertTrue(await repeated_identity_failure(conn,self.job,self.attempt,issues))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
