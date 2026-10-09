import unittest
from dataclasses import replace

from core.ensemble import adaptive_weights, pool
from service.runtime_settings import RuntimeConfig


class EnsembleTests(unittest.TestCase):
    def test_sparse_prior_clusters_and_weight_cap(self):
        empty=adaptive_weights(['a','b'],{'rows':[]})
        self.assertEqual(empty['weights'],{'a':.5,'b':.5})
        rows=[dict(algorithm='a',match_id=str(i),wins=1,losses=0,brier=.04,market_brier=.25) for i in range(40)]
        rows += [dict(algorithm='b',match_id=str(i),wins=0,losses=1,brier=.81,market_brier=.25) for i in range(40)]
        result=adaptive_weights(['a','b'],{'at':'past','rows':rows},20,.6)
        self.assertAlmostEqual(sum(result['weights'].values()),1)
        self.assertAlmostEqual(result['weights']['a'],.6)
        self.assertEqual(result['algorithms']['a']['matches'],40)

    def candidate(self, algorithm, **kw):
        c=dict(algorithm=algorithm,market='OU',line='2.5',outcome='over',odds=2,
               p_model=.7,ev=.4,effective_ev=.3,confidence=0,kelly=.2,research_only=False,tail=0)
        c.update(kw)
        return c

    def test_pool_one_decision_per_quote_and_portfolio_cap(self):
        evaluations=[{'algorithm':a,'candidates':[self.candidate(a),self.candidate(a,line='3.5')]} for a in ('a','b')]
        evidence=adaptive_weights(['a','b'],{'rows':[]})
        result=pool(evaluations,evidence,replace(RuntimeConfig(),max_total_exposure=.1))
        self.assertEqual(len(result['forecasts']),2)
        self.assertEqual(len(result['recommendations']),2)
        self.assertLessEqual(sum(c['kelly'] for c in result['recommendations']),.1)
        self.assertEqual(len(result['forecasts'][0]['members']),2)
        self.assertEqual(result['forecasts'][0]['p_model'],.7)

    def test_disagreement_and_research_only_reduce_confidence(self):
        evidence=adaptive_weights(['a','b'],{'rows':[]})
        evals=[{'algorithm':'a','candidates':[self.candidate('a',p_model=.9)]},
               {'algorithm':'b','candidates':[self.candidate('b',p_model=.1,research_only=True)]}]
        result=pool(evals,evidence,RuntimeConfig())
        self.assertEqual(result['recommendations'],[])
        self.assertLess(result['forecasts'][0]['confidence'],.5)
        self.assertGreater(result['forecasts'][0]['disagreement'],0)
