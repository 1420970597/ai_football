import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.settlement import summarise
from service.ledger import DecisionLedger, LedgerEntry


class LedgerDatabaseTests(unittest.TestCase):
    def row(self, **kw):
        values = dict(at='2026-10-01T12:00:00+00:00',match_id='m',decision_id='a',
                      market='OU',line='2.25',outcome='over',odds=2,is_pick=True,
                      algorithm='economic_ensemble',competition_type='real',trigger='live_forecast')
        values.update(kw)
        return LedgerEntry(**values)

    def test_migration_incremental_revisions_restart_and_pagination(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'ledger.jsonl'
            path.write_text(json.dumps(self.row().as_dict())+'\n'+'invalid\n')
            ledger=DecisionLedger(root)
            self.assertEqual(ledger.stats()['total'],1)
            revised=self.row(status='half_lost',pnl=-.5,updated_at='2026-10-09T12:00:00+00:00')
            with path.open('a') as f:
                f.write(json.dumps(revised.as_dict())+'\n')
            ledger._append([self.row(decision_id='b',match_id='n')])
            self.assertEqual(ledger.load()[0].status,'half_lost')
            again=DecisionLedger(root)
            self.assertEqual(again.stats()['total'],2)
            page=again.history(limit=1)
            self.assertEqual(page['next_offset'],1)
            self.assertEqual(again.history(limit=1,offset=1)['next_offset'],None)
            self.assertEqual(again._db.connection.execute('PRAGMA journal_mode').fetchone()[0],'wal')

    def test_sql_metrics_equal_settlement_and_frozen_returns(self):
        with tempfile.TemporaryDirectory() as root:
            ledger=DecisionLedger(root)
            rows=[self.row(status='won',pnl=1,confidence=.8,kelly=.1),
                  self.row(decision_id='b',status='half_lost',pnl=-.5,confidence=.4,kelly=.05),
                  self.row(decision_id='c',status='push',pnl=0),
                  self.row(decision_id='d',status='pending'),
                  self.row(decision_id='e',status='void')]
            ledger._append(rows)
            stats=ledger.stats()
            for key in ('total','graded','pending','void','won','lost','hit_rate','roi','stake_units','profit_units'):
                self.assertEqual(stats[key],summarise(rows)[key],key)
            self.assertAlmostEqual(stats['confidence_roi'],.5)
            self.assertAlmostEqual(stats['allocated_roi'],.5)
            self.assertEqual(stats['accuracy_samples'],2)
            self.assertEqual(stats['accuracy'],.6667)
            with patch.object(ledger._db,'entries',side_effect=AssertionError('full payload scan')):
                self.assertEqual(ledger.stats()['total'],5)

    def test_recommendation_performance_exposes_pending_without_counting_it(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = DecisionLedger(root)
            ledger._append([
                self.row(decision_id='hit', status='won', trigger='live_recommendation'),
                self.row(decision_id='miss', status='half_lost', trigger='live_recommendation'),
                self.row(decision_id='open', status='pending', trigger='live_recommendation'),
            ])
            evidence = ledger.recommendation_performance('OU', '2.25', 'over')
            self.assertEqual(evidence['hit_count'], 1.0)
            self.assertEqual(evidence['miss_count'], .5)
            self.assertEqual(evidence['pending_count'], 1)
            self.assertAlmostEqual(evidence['accuracy'], 2 / 3, places=6)

    def test_live_recommendations_restores_latest_pick_by_match(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = DecisionLedger(root)
            ledger._append([self.row(decision_id='r1', trigger='live_recommendation',
                                     at='2026-10-01T12:00:00+00:00'),
                            self.row(decision_id='r2', trigger='live_recommendation',
                                     at='2026-10-01T12:01:00+00:00', odds=2.1)])
            restored = ledger.live_recommendations(['m'])['m']
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0]['odds'], 2.1)
            self.assertEqual(ledger.live_recommendations(['missing']), {})

    def test_portfolio_excludes_algorithm_duplicates_but_ignores_display_cohort(self):
        with tempfile.TemporaryDirectory() as root:
            ledger=DecisionLedger(root)
            ledger._append([self.row(status='won',pnl=1),
                            self.row(decision_id='b',status='won',pnl=1,trigger='live_recommendation'),
                            self.row(decision_id='c',status='won',pnl=1,trigger='live_recommendation',algorithm='poisson_market')])
            result=ledger.history(cohort='prospective')
            self.assertEqual(result['overall']['total'],1)
            self.assertEqual(result['portfolio_summary']['stake_units'],1)
            self.assertEqual(result['portfolio_summary']['profit_units'],1)

    def test_half_score_can_be_retried_and_regrade_stays_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            ledger=DecisionLedger(root)
            ledger._append([self.row(market='OU_1H',line='1.5')])
            self.assertEqual(ledger.settle({'m':{'ft':[2,1],'done':True}})['settled'],0)
            self.assertEqual(ledger.load()[0].status,'pending')
            ledger.settle({'m':{'ft':[2,1],'ht':[1,1],'done':True}})
            self.assertEqual(ledger.stats()['accuracy'],1)
            ledger.settle({'m':{'ft':[2,1],'ht':[0,0],'done':True}},regrade=True)
            self.assertEqual(ledger.stats()['accuracy'],0)
            self.assertEqual(ledger.stats()['total'],1)

    def test_evidence_uses_only_earlier_settled_real_prospective_matches(self):
        with tempfile.TemporaryDirectory() as root:
            ledger=DecisionLedger(root)
            rows=[self.row(status='won',p_fused=.7,p_market=.5,settled_at='2026-10-02T00:00:00Z'),
                  self.row(decision_id='b',status='lost',p_fused=.7,p_market=.5,settled_at='2999-01-01T00:00:00Z'),
                  self.row(decision_id='c',status='won',competition_type='virtual',settled_at='2026-10-02T00:00:00Z'),
                  self.row(decision_id='d',status='won',trigger='live_recommendation',settled_at='2026-10-02T00:00:00Z')]
            ledger._append(rows)
            self.assertEqual(len(ledger.performance_evidence()['rows']),1)

    def test_paired_llm_abstention_and_error_do_not_create_fake_returns(self):
        with tempfile.TemporaryDirectory() as root:
            ledger=DecisionLedger(root)
            row={'computed_at':'2026-10-01T12:00:00+00:00','match_id':'m','competition_type':'real',
                 'score':[0,0],'elapsed_s':100,'config_version':2,'model_version':'test',
                 'forecasts':[{'market':'HAD','line':'','outcome':'home','odds':2,'p_model':.6,
                               'p_market':.5,'confidence':.7,'kelly':.1,'settlement_basis':'full_score',
                               'decision_status':'settleable'}]}
            ledger.record_experiment(row,{'status':'pending'})
            ledger.record_experiment(row,{'status':'ready','verdict':'reject','confidence':.9})
            ledger.settle({'m':{'ft':[1,0],'done':True}})
            result=ledger.experiment_stats('real')
            self.assertEqual(result['paired_opportunities'],1)
            self.assertEqual(result['coverage'],0)
            self.assertEqual(result['profit_difference'],-1)
            ledger.record_experiment({**row,'computed_at':'2026-10-01T12:05:00+00:00'}, {'status':'ready','verdict':'confirm'})
            self.assertEqual(ledger.stats()['total'],1)  # only one staked arm
            self.assertEqual(len(ledger.load()),2)
            ledger.record_experiment(row,{'status':'error','error_code':'upstream_http_502'})
            result=ledger.experiment_stats('real')
            self.assertEqual(result['paired_opportunities'],0)
            self.assertIsNone(result['profit_difference'])
            self.assertEqual(result['errors'],{'upstream_http_502':1})
