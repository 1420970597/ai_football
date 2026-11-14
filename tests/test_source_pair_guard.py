from unittest.mock import AsyncMock
import unittest
import uuid
from app.review_team_identity import explicit_source_pairs,source_pair_supports_claim
from app.review_resolution import resolve_candidates
from app.routes.reviews import ResolveInput


class SourcePairGuardTests(unittest.IsolatedAsyncioTestCase):
    def rows(self):
        return [{'candidate_data':{'sport':'football','competition_name':'日本足球联赛',
            'home_team':'VONDS市原','away_team':'杰里斯FC'}}]

    def test_original_pair_and_verified_alias_survive_but_candidate_filling_does_not(self):
        pairs=explicit_source_pairs('12:00 日本足球联赛\n市原VS杰里斯\n全场客+0.25')
        self.assertEqual([('市原','杰里斯')],pairs)
        self.assertTrue(source_pair_supports_claim('市原','杰里斯',pairs,self.rows()))
        self.assertFalse(source_pair_supports_claim('VONDS市原','杰里斯FC',pairs,self.rows()))
        nba=[{'candidate_data':{'sport':'basketball','competition_name':'NBA'}}]
        self.assertTrue(source_pair_supports_claim('萨克拉门托国王','波特兰开拓者',
            [('国王','开拓者')],nba))
        self.assertFalse(source_pair_supports_claim('萨克拉门托国王','波特兰开拓者',
            [('国王','开拓者')],self.rows()))

    async def test_refilled_pair_cannot_create_binding(self):
        conn=AsyncMock();conn.fetchrow.return_value={'raw_text':'市原VS杰里斯','media_objects':[]}
        body=ResolveInput(review_id=uuid.uuid4(),attempt_id=uuid.uuid4(),search_key='a'*64,
            team_a='VONDS市原',team_b='杰里斯FC')
        value=await resolve_candidates(conn,body,self.rows())
        self.assertEqual('source_team_identity_unverified',value['reason'])
        self.assertEqual(1,conn.fetchrow.await_count)

    def test_absent_pair_is_not_inferred_from_selection_or_time(self):
        self.assertEqual([],explicit_source_pairs('市原全场大2.5\n12:00 比赛预告'))
        self.assertEqual([],explicit_source_pairs('雪兰莪 U20 VS 柔佛达鲁塔克欣C队 U20 全场大2.5'))
