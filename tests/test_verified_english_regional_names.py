"""Publisher-backed whole Chinese labels; no youth or sport identity widening."""
import os
import unittest
from app import team_matching as names
import test_reviews as support


class RegionalWholeLabelTests(unittest.TestCase):
    def test_exact_regional_labels_keep_sport_and_squad_boundaries(self):
        for source,target in (('般尼茅夫','伯恩茅斯'),('葉士域治','伊普斯维奇')):
            self.assertTrue(names.team_name_matches(source,target,sport='football'))
            for sport in (None,'basketball'):
                self.assertFalse(names.team_name_matches(source,target,sport=sport))
            for suffix in ('U21','B队','女'):
                self.assertFalse(names.team_name_matches(source,target+suffix,sport='football'))
        self.assertFalse(names.team_name_matches('般尼','伯恩茅斯',sport='football'))
        self.assertFalse(names.team_name_matches('葉士域治','普利茅斯',sport='football'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires task-owned isolated database')
class RegionalAliasAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_omitted_sport_and_reversed_source_order_preserve_identity_evidence(self):
        wanted=await self.fixture(1,home_team='伊普斯维奇',away_team='伯恩茅斯')
        await self.fixture(2,home_team='伊普斯维奇',away_team='伯恩茅斯',sport='basketball')
        await self.fixture(3,home_team='伊普斯维奇U21',away_team='伯恩茅斯U21')
        response=await self.post('search_matches',team_query='般尼茅夫',team_a='般尼茅夫',team_b='葉士域治')
        self.assertEqual(200,response.status_code,response.text)
        result=response.json()
        self.assertTrue(result['resolution']['resolved'])
        self.assertEqual(wanted,result['resolution']['fixture']['id'])
        self.assertEqual('7m:18:20261108',result['resolution']['fixture']['verified_alias_sources']['home'])
        self.assertEqual('7m:674:20261108',result['resolution']['fixture']['verified_alias_sources']['away'])
        row=await self.pool.fetchrow('SELECT home_team,away_team,home_aliases,away_aliases FROM v2_fixtures WHERE id=$1',wanted)
        self.assertEqual(('伊普斯维奇','伯恩茅斯',[],[]),tuple(row))
        evidence=await self.pool.fetchval('SELECT evidence FROM v2_review_fixture_bindings WHERE job_id=$1 AND fixture_id=$2',self.job,wanted)
        self.assertEqual(result['resolution']['fixture']['verified_alias_sources'],evidence['verified_alias_sources'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_other_side_search_recalls_but_duplicate_fixture_is_not_auto_bound(self):
        wanted=await self.fixture(1,home_team='伊普斯维奇',away_team='伯恩茅斯')
        response=await self.post('search_matches',team_query='葉士域治',sport='football',
                                 team_a='葉士域治',team_b='般尼茅夫')
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(wanted,response.json()['resolution']['fixture']['id'])
        await self.fixture(2,home_team='伊普斯维奇',away_team='伯恩茅斯')
        response=await self.post('search_matches',team_query='伊普斯维奇',sport='football',
                                 team_a='葉士域治',team_b='般尼茅夫')
        self.assertEqual(200,response.status_code,response.text)
        self.assertFalse(response.json()['resolution']['resolved'])
        self.assertEqual('ambiguous_fixture',response.json()['resolution']['reason'])
