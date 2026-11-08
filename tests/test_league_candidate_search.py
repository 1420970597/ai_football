"""Image transliterations can inspect a bounded league without inventing identities."""
from datetime import timedelta
import unittest

from pydantic import ValidationError

from app.routes.reviews import SearchInput
import test_reviews as fixture


class LeagueCandidateScopeTests(unittest.TestCase):
    def scope(self, **updates):
        return dict(review_id='11111111-1111-4111-8111-111111111111',
            attempt_id='22222222-2222-4222-8222-222222222222', team_query='',
            competition_query='捷克丙级联赛', sport='football',
            from_utc='2026-11-08T00:00:00Z', to_utc='2026-11-09T00:00:00Z', **updates)

    def test_empty_team_query_requires_explicit_bounded_league_scope(self):
        self.assertEqual('', SearchInput(**self.scope()).team_query)
        for changed in ({'competition_query':None}, {'competition_query':'  '},
                        {'competition_query':'捷'}, {'competition_query':'%%'},
                        {'competition_query':'__'}, {'sport':None}, {'from_utc':None},
                        {'to_utc':None}, {'to_utc':'2026-11-16T00:00:00Z'},
                        {'to_utc':'2026-11-08T00:00:00Z'}):
            with self.subTest(changed=changed),self.assertRaises(ValidationError):
                SearchInput(**(self.scope()|changed))

    def test_seven_days_allowed_but_one_character_team_stays_invalid(self):
        body=SearchInput(**(self.scope()|{'to_utc':'2026-11-15T00:00:00Z'}))
        self.assertEqual('all',body.phase)
        with self.assertRaises(ValidationError):
            SearchInput(**(self.scope()|{'team_query':'A'}))


@unittest.skipUnless(fixture.os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires task-owned isolated database')
class LeagueCandidateSearchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.ReviewTests.asyncSetUp
    asyncTearDown=fixture.ReviewTests.asyncTearDown
    context=fixture.ReviewTests.context
    post=fixture.ReviewTests.post
    fixture=fixture.ReviewTests.fixture

    async def test_complete_league_pages_recall_image_variant_without_binding_other_match(self):
        league='捷克丙级联赛'
        wanted=await self.fixture(1,home_team='赫拉德茨克拉洛韦B队',away_team='恰斯拉夫',competition_name=league)
        unrelated=await self.fixture(2,home_team='兹林B队',away_team='弗雷德克',competition_name=league)
        await self.fixture(3,home_team='赫拉德茨克拉洛韦B队',away_team='恰斯拉夫',competition_name='其他联赛')
        await self.fixture(4,competition_name=league,sport='basketball')
        await self.fixture(5,competition_name=league,kickoff_at=(self.now+timedelta(days=8)).isoformat())
        scope=dict(team_query='',competition_query=league,sport='football',page_size=1,
            team_a='贺拉戴克B队',team_b='卡拉夫',
            from_utc=(self.now-timedelta(hours=12)).isoformat(),
            to_utc=(self.now+timedelta(hours=72)).isoformat())
        r=await self.post('search_matches',**scope)
        self.assertEqual(200,r.status_code,r.text)
        first=r.json();self.assertEqual(2,first['total_candidates'])
        self.assertFalse(first['search_complete'])
        premature=await self.post('resolve_match',search_key=first['search_key'],
            team_a='赫拉德茨克拉洛韦B队',team_b='恰斯拉夫')
        self.assertEqual(409,premature.status_code)
        changed=await self.post('search_matches',**(scope|{'sport':'basketball','cursor':first['next_cursor']}))
        self.assertEqual(409,changed.status_code)
        second=await self.post('search_matches',**scope,cursor=first['next_cursor'])
        self.assertEqual(200,second.status_code,second.text)
        final=second.json();self.assertTrue(final['search_complete'])
        self.assertFalse(final['resolution']['resolved'])
        self.assertIsNone(final['next_cursor'])
        items=first['items']+final['items']
        self.assertEqual({wanted,unrelated},{r['id']for r in items})
        self.assertTrue(all(r['retrieval_basis']=='competition_time_candidate' for r in items))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        unread=await self.post('list_match_markets',fixture_id=unrelated)
        self.assertEqual(409,unread.status_code)
        resolved=await self.post('resolve_match',search_key=first['search_key'],
            team_a='赫拉德茨克拉洛韦B队',team_b='恰斯拉夫',competition=league)
        self.assertTrue(resolved.json()['resolved'])
        self.assertEqual(wanted,resolved.json()['fixture']['id'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_unbounded_or_incomplete_league_request_creates_no_search(self):
        scope=dict(team_query='',competition_query='捷克丙级联赛',sport='football',
            from_utc=self.now.isoformat(),to_utc=(self.now+timedelta(days=1)).isoformat())
        for changed in ({'sport':None},{'competition_query':None},{'from_utc':None},
                        {'to_utc':(self.now+timedelta(days=7,seconds=1)).isoformat()}):
            r=await self.post('search_matches',**(scope|changed))
            self.assertEqual(422,r.status_code,r.text)
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_match_searches'))
