import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.settlement import summarise
from core.live_model import MODEL_VERSION
from service.ledger import DecisionLedger, LedgerEntry
from store.ledger_db import LedgerDB


class LedgerDatabaseTests(unittest.TestCase):
    def test_history_read_does_not_rescan_archived_jsonl_after_projection_sync(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = DecisionLedger(root)
            ledger._append([self.row(trigger='live_recommendation', status='won')])
            self.assertTrue(ledger._db_synchronized)
            with patch.object(ledger, '_files', side_effect=AssertionError('archive rescan')):
                result = ledger.history(limit=10)
            self.assertEqual(result['overall']['total'], 1)

    def test_betting_evidence_reads_committed_projection_while_writer_is_busy(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = DecisionLedger(root)
            ledger._append([self.row(trigger='live_recommendation', status='won')])
            entered, release = threading.Event(), threading.Event()
            def writer():
                with ledger._lock:
                    entered.set()
                    release.wait(5)
            thread = threading.Thread(target=writer)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                result = []
                read = threading.Thread(target=lambda: result.append(
                    ledger.recommendation_performance('OU', '2.25', 'over')))
                read.start()
                read.join(2)
                self.assertFalse(read.is_alive(), '投注命中查询不得等待台账写锁')
                self.assertEqual(result[0]['hit_count'], 1)
            finally:
                release.set()
                thread.join(5)
                read.join(5)

    def test_old_quote_projection_migrates_without_losing_evidence(self):
        import sqlite3
        from store.ledger_db import FIELDS
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'ledger.sqlite3'
            fields = {k: v for k, v in FIELDS.items() if k not in ('line', 'outcome', 'model_version')}
            row = self.row(trigger='live_recommendation', status='won').as_dict()
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE decisions (identity TEXT PRIMARY KEY, revision REAL, payload TEXT,' +
                           ','.join(k + ' ' + v for k, v in fields.items()) + ')')
                values = ['old', 0, json.dumps(row), *[row.get(k) for k in fields]]
                db.execute('INSERT INTO decisions VALUES (' + ','.join('?' for _ in values) + ')', values)
            ledger = DecisionLedger(root)
            self.assertEqual(ledger.recommendation_performance('OU', '2.25', 'over')['hit_count'], 1)
            projected = ledger._db.connection.execute('SELECT line,outcome FROM decisions').fetchone()
            self.assertEqual(tuple(projected), ('2.25', 'over'))
            plan = ledger._db.connection.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM decisions WHERE trigger='live_recommendation' "
                "AND is_pick=1 AND algorithm=? AND market=? AND line=? AND outcome=?",
                ('economic_ensemble', 'OU', '2.25', 'over')).fetchall()
            self.assertTrue(any('idx_recommendation_quote' in r[3] for r in plan))

    def test_slow_evidence_does_not_hold_writer_or_betting_evidence_lock(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = DecisionLedger(root)
            ledger._append([self.row(trigger='live_recommendation', status='won')])
            entered, release, written = threading.Event(), threading.Event(), threading.Event()
            original = LedgerDB.evidence
            errors = []
            def slow(db, cutoff):
                entered.set()
                release.wait(5)
                return original(db, cutoff)
            def write():
                try:
                    ledger._append([self.row(decision_id='b')])
                    ledger.recommendation_performance('OU', '2.25', 'over')
                    written.set()
                except Exception as exc:
                    errors.append(exc)
            with patch.object(LedgerDB, 'evidence', slow):
                reader = threading.Thread(target=ledger.performance_evidence)
                reader.start()
                self.assertTrue(entered.wait(2))
                writer = threading.Thread(target=write)
                writer.start()
                try:
                    self.assertTrue(written.wait(2), 'aggregation blocked live writers/checks')
                finally:
                    release.set()
                    reader.join(5)
                    writer.join(5)
            self.assertEqual(errors, [])
            self.assertEqual(ledger.stats()['total'], 2)

    def test_read_projection_is_read_only_and_memory_mode_remains_supported(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as root:
            ledger = DecisionLedger(root)
            with ledger._read_db() as reader:
                with self.assertRaises(sqlite3.OperationalError):
                    reader.connection.execute('DELETE FROM decisions')
        ledger = DecisionLedger()
        self.assertEqual(ledger.stats()['total'], 0)
        self.assertEqual(ledger.performance_evidence()['rows'], [])

    def row(self, **kw):
        values = dict(at='2026-10-01T12:00:00+00:00',match_id='m',decision_id='a',
                      market='OU',line='2.25',outcome='over',odds=2,is_pick=True,
                      algorithm='economic_ensemble',competition_type='real',trigger='live_forecast',model_version=MODEL_VERSION)
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

    def test_late_observed_label_backfill_is_idempotent_and_does_not_backdate(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = DecisionLedger(root)
            first = self.row(match_id='m1', status='won', updated_at='2026-10-08T12:00:00+00:00')
            first.ft_score = [2, 1]
            first.ht_score = [1, 0]
            ledger._append([first])
            result = ledger._db.backfill_observed_labels('2026-10-11T12:00:00+00:00')
            self.assertEqual(result['backfilled_matches'], 1)
            self.assertEqual(ledger._db.connection.execute('SELECT count(*) FROM label_revisions').fetchone()[0], 1)
            available = ledger._db.connection.execute(
                'SELECT available_at FROM label_revisions WHERE match_id=?', ('m1',)).fetchone()[0]
            self.assertEqual(available, '2026-10-11T12:00:00+00:00')
            again = ledger._db.backfill_observed_labels('2026-10-11T12:05:00+00:00')
            self.assertEqual(again['backfilled_matches'], 0)
            self.assertEqual(ledger._db.connection.execute('SELECT count(*) FROM label_revisions').fetchone()[0], 1)

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

    def test_repeated_live_decisions_increment_occurrences_without_fake_hits(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = DecisionLedger(root)

            def live(event_id, computed_at):
                return {
                    'match_id': 'm', 'model_version': MODEL_VERSION, 'config_version': 1,
                    'competition_type': 'real', 'computed_at': computed_at,
                    'score': [0, 0], 'elapsed_s': 120, 'publication_id': event_id,
                    'forecast': {'market': 'OU', 'line': '2.25', 'outcome': 'over',
                                 'odds': 2, 'ev': .1, 'p_market': .5, 'p_model': .6,
                                 'settlement_basis': 'legacy_full_score',
                                 'decision_status': 'settleable'},
                    'algorithm': 'economic_ensemble', 'record_kind': 'recommendation',
                    'decision_event_id': event_id,
                }

            self.assertEqual(ledger.record_live_many([live('p1', '2026-10-01T12:00:00+00:00')]), 1)
            self.assertEqual(ledger.record_live_many([live('p2', '2026-10-01T12:00:01+00:00')]), 1)
            evidence = ledger.recommendation_performance('OU', '2.25', 'over')
            self.assertEqual(evidence['decision_count'], 2)
            self.assertEqual(evidence['decision_occurrences'], 2)
            self.assertEqual(evidence['hit_count'], 0)
            self.assertEqual(evidence['pending_decisions'], 2)

            # Retrying the same publication must not increment the counter again.
            self.assertEqual(ledger.record_live_many([live('p2', '2026-10-01T12:00:01+00:00')]), 0)
            self.assertEqual(ledger.recommendation_performance('OU', '2.25', 'over')['decision_count'], 2)

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

    def test_calibration_generations_cannot_borrow_baseline_hits(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = DecisionLedger(root)
            ledger._append([
                self.row(decision_id='base',status='won',trigger='live_recommendation',pnl=1),
                self.row(decision_id='v1',status='lost',trigger='live_recommendation',pnl=-1,decision_model_version='v1'),
                self.row(decision_id='v2',status='won',trigger='live_recommendation',pnl=1,decision_model_version='v2'),
            ])
            baseline = ledger.recommendation_performance('OU','2.25','over')
            calibrated = ledger.recommendation_performance('OU','2.25','over',decision_model_version='v1')
            self.assertEqual(baseline['hit_count'],1)
            self.assertEqual(calibrated['hit_count'],0)
            self.assertEqual(calibrated['roi'],-1)
            self.assertEqual(calibrated['calibration_evidence_version'],'v1')
            self.assertEqual(ledger.recommendation_performance('OU','2.25','over',decision_model_version='unknown')['settled_samples'],0)

    def test_recommendation_entries_include_pending_and_settled(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = DecisionLedger(root)
            ledger._append([self.row(decision_id='open', trigger='live_recommendation', status='pending'),
                            self.row(decision_id='done', trigger='live_recommendation', status='won'),
                            self.row(decision_id='individual', trigger='live_recommendation',
                                     algorithm='poisson_market', status='won')])
            entries = ledger.recommendation_entries('real')
            self.assertEqual({entry['status'] for entry in entries}, {'pending', 'won'})
            self.assertTrue(all(entry['algorithm'] == 'economic_ensemble' for entry in entries))

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
