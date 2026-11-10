"""Same-publisher same-ID Belgian whole labels, with strict squad boundaries."""
import os
import unittest
from app import team_matching as names
import test_reviews as support


class BelgianWholeLabelTests(unittest.TestCase):
    def test_publisher_labels_require_football_and_exact_squad(self):
        for source,target in (('真特','根特'),('哥積克','科特赖克')):
            self.assertTrue(names.team_name_matches(source,target,sport='football'))
            for sport in (None,'basketball'):
                self.assertFalse(names.team_name_matches(source,target,sport=sport))
            for suffix in ('U21','B队','女'):
                self.assertFalse(names.team_name_matches(source,target+suffix,sport='football'))
        self.assertFalse(names.team_name_matches('哥積','科特赖克',sport='football'))
        self.assertFalse(names.team_name_matches('真特','亨克',sport='football'))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires task-owned isolated database')
class BelgianAliasAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture

    async def test_reversed_original_names_bind_only_actual_football_squad_with_provenance(self):
        wanted=await self.fixture(1,home_team='根特',away_team='科特赖克')
        await self.fixture(2,home_team='根特',away_team='科特赖克',sport='basketball')
        await self.fixture(3,home_team='根特U21',away_team='科特赖克U21')
        response=await self.post('search_matches',team_query='哥積克',team_a='哥積克',team_b='真特')
        self.assertEqual(200,response.status_code,response.text)
        result=response.json();self.assertTrue(result['resolution']['resolved'])
        self.assertEqual(wanted,result['resolution']['fixture']['id'])
        refs=result['resolution']['fixture']['verified_alias_sources']
        self.assertEqual({'registry_version':'2026-11-10.2',
                          'home':'7m:7:20261108','away':'7m:1609:20261108'},refs)
        stored=await self.pool.fetchrow('SELECT home_team,away_team,home_aliases,away_aliases FROM v2_fixtures WHERE id=$1',wanted)
        self.assertEqual(('根特','科特赖克',[],[]),tuple(stored))
        evidence=await self.pool.fetchval('SELECT evidence FROM v2_review_fixture_bindings WHERE job_id=$1 AND fixture_id=$2',self.job,wanted)
        self.assertEqual(refs,evidence['verified_alias_sources'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_other_side_recall_does_not_override_same_pair_ambiguity(self):
        wanted=await self.fixture(1,home_team='根特',away_team='科特赖克')
        page=await self.post('search_matches',team_query='真特',sport='football',team_a='真特',team_b='哥積克')
        self.assertTrue(page.json()['resolution']['resolved'],page.text)
        self.assertEqual(wanted,page.json()['resolution']['fixture']['id'])
        await self.fixture(2,home_team='根特',away_team='科特赖克')
        page=await self.post('search_matches',team_query='根特',sport='football',team_a='真特',team_b='哥積克')
        self.assertFalse(page.json()['resolution']['resolved'])
        self.assertEqual('ambiguous_fixture',page.json()['resolution']['reason'])
        self.assertEqual(2,page.json()['resolution']['candidate_count'])
