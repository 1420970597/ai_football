"""Candidate observation is prospective, versioned and never execution approval."""
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from core.learned_model import DATA_VERSION, FEATURES
from core.decision_features import FEATURE_VERSION
from core.live_model import MODEL_VERSION
from service.model_service import ModelService, hashlib_version
from service.model_training import atomic_json
from store.training_data import (_shadow_metrics, _prospective_metrics_postgres,
                                 _shadow_label_subset, labels, prospective_prediction_rows,
                                 _sample_shadow_contracts, _shadow_label_events)
from store.training_projection import ProjectionTarget
from tests.test_training_data import decision


class ShadowObservationTests(unittest.TestCase):
    def artifact(self, version='candidate-v1', **changes):
        return {'version': version, 'features': list(FEATURES), 'weights': [0.] * len(FEATURES),
                'data_version': DATA_VERSION, 'feature_version': FEATURE_VERSION,
                'updated_at': '2025-01-01T10:00:00+00:00', 'parameter_count': len(FEATURES),
                'promotion': {'approved': False, 'reasons': ['insufficient evidence']}, **changes}

    def source_row(self, **changes):
        return {'version': 'candidate-v1', 'match_id': 'm1', 'at': '2025-01-01T12:00:00+00:00',
                'predicted_at': '2025-01-01T12:00:01+00:00', 'cutoff_ms': 1735732800000.,
                'decision_received_at_ms': 1735732800000., 'quote_received_at_ms': 1735732800000.,
                'algorithm': 'economic_ensemble', 'market': 'OU', 'line': '1/1.5', 'outcome': 'under',
                'settlement_basis': 'full_score', 'entry_score': '[0,0]', 'x': '[0]',
                'baseline': .4, 'probability': .8, 'model_version': MODEL_VERSION,
                'data_version': DATA_VERSION, 'feature_version': FEATURE_VERSION, **changes}

    def test_unapproved_candidate_observes_without_active_or_execution_availability(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd() / 'output', prefix='shadow-test-') as directory:
            root = Path(directory)
            model = self.artifact()
            atomic_json(root/'models'/'candidate-candidate-v1.json', model)
            worker = ModelService()
            worker.bind(str(root/'ledger'))
            row = decision()
            row.update(input_cutoff_ms=1735732800000., received_at_ms=1735732800000.)
            for evaluation in row['evaluations']:
                for candidate in evaluation['candidates']:
                    candidate.update(ts_ms=1735732800000., quote_received_at_ms=1735732800000.)
            with patch('service.model_service.time.time', return_value=1735732801):
                worker.observe(row)
            self.assertEqual(len(worker._shadow), 8)
            status = worker.status()
            self.assertFalse(status['decision_model_available'])
            self.assertFalse(status['model'])
            self.assertTrue(status['shadow']['available'])
            self.assertFalse(status['shadow']['approved'])
            self.assertEqual(status['shadow']['artifact_kind'], 'candidate')
            self.assertFalse((root/'models'/'active.json').exists())
            old_row = {**row, 'input_cutoff_ms': 1000}
            worker.observe(old_row)
            self.assertEqual(len(worker._shadow), 8)
            atomic_json(root/'models'/'candidate-candidate-v2.json', self.artifact('candidate-v2'))
            worker._live_metrics = {'version': 'candidate-v1', 'model': {'accuracy': .9}}
            worker._read()
            self.assertEqual(worker.status()['shadow']['version'], 'candidate-v2')
            self.assertFalse(worker.status()['prospective'])

    def test_disabled_shadow_and_invalid_candidate_do_not_generate_predictions(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory(dir=Path.cwd() / 'output', prefix='shadow-disabled-') as directory:
            root = Path(directory)
            atomic_json(root/'models'/'candidate-v1.json', self.artifact(updated_at='2999-01-01T00:00:00+00:00'))
            worker = ModelService()
            worker.bind(str(root/'ledger'))
            self.assertFalse(worker.status()['shadow']['available'])
            worker.observe(decision())
            self.assertFalse(worker._shadow)
            self.assertIn('尚未可用',worker.status()['shadow']['reason'])
            atomic_json(root/'models'/'candidate-v1.json', self.artifact())
            worker._read()
            worker.configure(replace(worker.cfg,model_shadow_enabled=False))
            worker.observe(decision())
            self.assertFalse(worker._shadow)
            self.assertFalse(worker.status()['shadow']['enabled'])

    def test_evaluation_snapshot_persists_current_version(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()/'output',prefix='shadow-evaluation-') as directory:
            root = Path(directory)
            atomic_json(root/'models'/'candidate-v1.json',self.artifact())
            worker = ModelService()
            worker.bind(str(root/'ledger'))
            worker._evaluation_state = {'version':'candidate-v1','rows':[]}
            original_state = worker._evaluation_state
            def evaluate(*args,**kwargs):
                self.assertIsNot(kwargs['state'],original_state)
                kwargs['state']['rows'].append({'match_id':'m1'})
                return {'version':'candidate-v1','status':'waiting_for_settlement','cursor':{'has_more':True}}
            with patch('service.model_service.prospective_metrics',side_effect=evaluate):
                worker._evaluate_shadow()
            self.assertFalse(original_state['rows'])
            self.assertEqual(worker._evaluation_state['rows'],[{'match_id':'m1'}])
            self.assertEqual(json.loads((root/'models'/'prospective.json').read_text()),worker._live_metrics)
            state_path = root/'models'/('shadow-window-'+hashlib_version('candidate-v1')+'.json')
            self.assertEqual(json.loads(state_path.read_text()),worker._evaluation_state)
            self.assertGreater(worker._next_evaluation,0)

    def test_candidate_switch_during_pg_evaluation_discards_old_result_and_error(self):
        for failure in (False,True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(
                    dir=Path.cwd()/'output',prefix='shadow-switch-') as directory:
                root = Path(directory)
                atomic_json(root/'models'/'candidate-v1.json',self.artifact())
                worker = ModelService()
                worker.bind(str(root/'ledger'))
                worker._evaluation_state = {'version':'candidate-v1','rows':[]}
                original_state = worker._evaluation_state
                entered,release = threading.Event(),threading.Event()
                def evaluate(*args,entered=entered,release=release,failure=failure,**kwargs):
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError('test did not release evaluation')
                    kwargs['state']['rows'].append({'match_id':'old-match'})
                    if failure:
                        raise RuntimeError('old candidate query failed')
                    return {'version':'candidate-v1','status':'ready'}
                with patch('service.model_service.prospective_metrics',side_effect=evaluate):
                    evaluation = threading.Thread(target=worker._evaluate_shadow)
                    evaluation.start()
                    self.assertTrue(entered.wait(1))
                    # The live metadata reader must remain free while PG waits.
                    atomic_json(root/'models'/'candidate-v2.json',self.artifact('candidate-v2'))
                    switched = threading.Event()
                    def switch(worker=worker,switched=switched):
                        worker._read()
                        switched.set()
                    update = threading.Thread(target=switch)
                    update.start()
                    try:
                        self.assertTrue(switched.wait(1))
                    finally:
                        release.set()
                        evaluation.join(2)
                        update.join(2)
                    self.assertFalse(evaluation.is_alive())
                    self.assertFalse(update.is_alive())
                self.assertEqual(worker._shadow_model['version'],'candidate-v2')
                self.assertFalse(worker._evaluation_state)
                self.assertFalse(worker._live_metrics)
                self.assertEqual(worker._next_evaluation,0)
                self.assertFalse(original_state['rows'])
                self.assertFalse((root/'models'/'prospective.json').exists())
                self.assertFalse(list((root/'models').glob('shadow-window-*.json')))

    def test_candidate_reader_is_not_blocked_by_large_evaluation_window_write(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()/'output',prefix='shadow-window-write-') as directory:
            root = Path(directory)
            atomic_json(root/'models'/'candidate-v1.json',self.artifact())
            worker = ModelService()
            worker.bind(str(root/'ledger'))
            entered,release,switched = threading.Event(),threading.Event(),threading.Event()
            def write(path,data):
                if path.name.startswith('shadow-window-'):
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError('test did not release window write')
                atomic_json(path,data)
            with patch('service.model_service.prospective_metrics',return_value={'version':'candidate-v1'}), \
                    patch('service.model_service.atomic_json',side_effect=write):
                evaluation = threading.Thread(target=worker._evaluate_shadow)
                evaluation.start()
                self.assertTrue(entered.wait(1))
                atomic_json(root/'models'/'candidate-v2.json',self.artifact('candidate-v2'))
                def switch():
                    worker._read()
                    switched.set()
                update = threading.Thread(target=switch)
                update.start()
                try:
                    self.assertTrue(switched.wait(1))
                finally:
                    release.set()
                    evaluation.join(2)
                    update.join(2)
                self.assertFalse(evaluation.is_alive())
                self.assertFalse(update.is_alive())
            self.assertEqual(worker._shadow_model['version'],'candidate-v2')
            self.assertFalse(worker._evaluation_state)
            self.assertFalse(worker._live_metrics)
            self.assertEqual(worker._next_evaluation,0)
            self.assertFalse((root/'models'/'prospective.json').exists())
            self.assertEqual(len(list((root/'models').glob('shadow-window-*.json'))),1)

    def test_pending_zero_sample_unknown_and_metrics_use_saved_probability_not_weights(self):
        rows = [self.source_row(), self.source_row(outcome='over', probability=.2, baseline=.6)]
        pending = _shadow_metrics(rows, {}, self.artifact())
        self.assertEqual(pending['counts']['pending_predictions'], 2)
        self.assertIsNone(pending['model']['accuracy'])
        self.assertEqual(pending['status'], 'waiting_for_settlement')
        final = {'m1': {'ft_score': (1,0), 'ht_score': (1,0), 'settled_at': '2025-01-01T14:00:00+00:00'}}
        actual = _shadow_metrics(rows, final, self.artifact(weights=[999.] * len(FEATURES)))
        self.assertEqual(actual['counts']['settled_predictions'], 2)
        self.assertEqual(actual['model']['asian_payment_accuracy'], 1)
        self.assertEqual(actual['baseline']['asian_payment_accuracy'], 0)
        self.assertEqual(actual['model']['asian_selected_contracts'], 1)
        self.assertEqual(actual['latency']['p95_ms'], 1000)
        self.assertIsNone(actual['model']['had_top1_accuracy'])

    def test_had_top1_counts_one_contract_and_late_or_future_predictions_are_excluded(self):
        rows = [self.source_row(market='HAD', line='', outcome=outcome, probability=p,
                baseline=b) for outcome,p,b in (('home',.7,.2),('draw',.2,.5),('away',.1,.3))]
        finals = {'m1': {'ft_score': (1,0), 'ht_score': (1,0), 'settled_at':'2025-01-01T14:00:00+00:00'}}
        result = _shadow_metrics(rows, finals, self.artifact())
        self.assertEqual(result['model']['had_top1_accuracy'],1)
        self.assertEqual(result['baseline']['had_top1_accuracy'],0)
        self.assertEqual(result['model']['complete_had_groups'],1)
        late = _shadow_metrics([self.source_row(predicted_at='2025-01-01T15:00:00+00:00')],finals,self.artifact())
        self.assertEqual(late['counts']['excluded_predictions'],1)
        self.assertIsNone(late['model']['accuracy'])
        future = _shadow_metrics([self.source_row()],finals,self.artifact(updated_at='2025-01-01T13:00:00+00:00'))
        self.assertEqual(future['counts']['excluded_predictions'],1)
        self.assertIsNone(future['model']['accuracy'])
        missing_clocks = _shadow_metrics([self.source_row(decision_received_at_ms=None)],finals,self.artifact())
        self.assertEqual(missing_clocks['counts']['excluded_predictions'],1)
        self.assertIsNone(missing_clocks['model']['accuracy'])

    def test_corrected_payment_uses_latest_label_but_first_availability_controls_eligibility(self):
        rows = [self.source_row(), self.source_row(outcome='over', probability=.2, baseline=.6)]
        latest = {'ft_score': (2,0), 'ht_score': (1,0), 'settled_at': '2025-01-01T15:00:00+00:00',
                  'first_label_available_at': '2025-01-01T14:00:00+00:00', 'label_version': 'correction-v2'}
        corrected = _shadow_metrics(rows, {'m1':latest}, self.artifact())
        self.assertEqual(corrected['model']['asian_payment_accuracy'],0)
        self.assertEqual(corrected['counts']['settled_predictions'],2)
        self.assertEqual(corrected['labels']['m1']['version'],'correction-v2')
        already_known = {**latest, 'first_label_available_at':'2025-01-01T11:00:00+00:00'}
        excluded = _shadow_metrics(rows, {'m1':already_known}, self.artifact())
        self.assertEqual(excluded['counts']['excluded_predictions'],2)
        self.assertEqual(excluded['counts']['settled_predictions'],0)
        self.assertIsNone(excluded['model']['accuracy'])

    def test_sqlite_earliest_actual_label_time_survives_nonmonotonic_source_order_and_invalid_labels(self):
        with sqlite3.connect(':memory:') as con:
            con.execute('CREATE TABLE label_revisions(match_id,available_at,ft_score,ht_score,status,revision_order,revision_id)')
            con.executemany('INSERT INTO label_revisions VALUES (?,?,?,?,?,?,?)', [
                ('m1','2025-01-01T15:00:00+00:00','[1,0]',None,'won',1,'later-arrival'),
                ('m1','2025-01-01T13:00:00+02:00','[2,0]',None,'lost',2,'earlier-real-time'),
                ('m1','invalid','[0,0]',None,'push',3,'bad-time'),
                ('m1','2025-01-01T10:00:00+00:00','[true,0]',None,'lost',4,'bad-score'),
                ('m1','2025-01-01T09:00:00+00:00','[0,0]',None,'pending',5,'not-terminal'),
            ])
            final = labels(con, ['m1'])['m1']
        self.assertEqual(final['first_label_available_at'],'2025-01-01T11:00:00+00:00')
        self.assertEqual(final['label_version'],'earlier-real-time')
        self.assertEqual(final['ft_score'],(2,0))

    def test_pg_label_subset_filters_before_windows_and_sorts_first_by_actual_time(self):
        statements = []
        value = {'match_id':'m1','available_at':'2025-01-01T15:00:00+00:00', 'ft_score':'[2,0]',
                 'ht_score':None,'revision_id':'latest','first_label_available_at':'2025-01-01T11:00:00+00:00'}
        class Cursor:
            description = [(key,) for key in value]
            def execute(self, query, parameters):
                statements.append((query, parameters))
            def fetchall(self):
                return [tuple(value.values())]
            def close(self):
                pass
        class Connection:
            def cursor(self):
                return Cursor()
        actual = _shadow_label_subset(Connection(),['m1'])
        self.assertEqual(actual['m1']['first_label_available_at'],'2025-01-01T11:00:00+00:00')
        self.assertEqual(actual['m1']['label_version'],'latest')
        sql, parameters = statements[0]
        self.assertIn('ORDER BY available_time,revision_id',sql)
        self.assertIn('ORDER BY revision_order DESC,revision_id DESC',sql)
        self.assertIn('pg_input_is_valid',sql)
        self.assertIn('CASE WHEN',sql)
        self.assertIn('available_time IS NOT NULL AND ft_score ~ %s',sql)
        self.assertEqual(parameters[1:3],('m1','m1'))

    def test_settled_replay_is_filtered_before_persistence_and_failed_label_read_retains_pending(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()/'output',prefix='shadow-replay-') as directory:
            root = Path(directory)/'ledger'
            root.mkdir()
            db = root/'ledger.sqlite3'
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE label_revisions(match_id,available_at,ft_score,ht_score,status,revision_order,revision_id)')
                con.executemany('INSERT INTO label_revisions VALUES (?,?,?,?,?,?,?)', [
                    ('m1','2025-01-01T11:00:00+00:00','[1,0]',None,'won',1,'first'),
                    ('m1','2025-01-01T15:00:00+00:00','[2,0]',None,'lost',2,'corrected'),
                ])
            candidates = [self.source_row(),self.source_row(match_id='new-match')]
            self.assertEqual([r['match_id'] for r in prospective_prediction_rows(db,candidates)],['new-match'])
            worker = ModelService()
            worker.root = root
            worker._shadow = {('old',):candidates[0],('new',):candidates[1]}
            with patch('service.model_service.prospective_prediction_rows',side_effect=TimeoutError('labels unavailable')):
                with self.assertRaises(TimeoutError):
                    worker._flush_shadow()
            self.assertEqual(len(worker._shadow),2)
            worker._flush_shadow()
            with sqlite3.connect(root.parent/'models'/'shadow.sqlite3') as con:
                self.assertEqual(con.execute('SELECT count(*) FROM predictions').fetchone()[0],1)
            self.assertEqual(worker._shadow_info['skipped_settled_replay'],1)
            self.assertFalse(worker._shadow)

    def test_pg_evaluation_is_bounded_keyset_and_resets_on_version_change(self):
        row = self.source_row()
        statements = []
        batches = [[row], []]

        class Cursor:
            description = [(name,) for name in row]
            def execute(self, operation, parameters):
                statements.append((operation, parameters))
            def fetchall(self):
                return [tuple(value[name] for name in row) for value in batches.pop(0)]
            def close(self):
                pass

        class Connection:
            def cursor(self):
                return Cursor()

        state = {}
        target = ProjectionTarget.parse('postgresql://localhost/model')
        with patch('store.training_data.readonly_snapshot', return_value=nullcontext(Connection())), \
                patch('store.training_data._shadow_label_subset', return_value={}) as labels, \
                patch('store.training_data._shadow_label_events',return_value=([],False)):
            first = _prospective_metrics_postgres(target,self.artifact(),state=state)
            second = _prospective_metrics_postgres(target,self.artifact(),state=state)
        self.assertEqual(first['counts']['predictions'],1)
        self.assertEqual(second['counts']['predictions'],1)
        self.assertIn('LIMIT %s',statements[0][0])
        self.assertIn(') > (',statements[1][0])
        self.assertFalse(any('model_inputs' in sql for sql,_ in statements))
        self.assertEqual(labels.call_args.args[1],['m1'])
        batches.append([])
        with patch('store.training_data.readonly_snapshot', return_value=nullcontext(Connection())), \
                patch('store.training_data._shadow_label_subset', return_value={}), \
                patch('store.training_data._shadow_label_events',return_value=([],False)):
            reset = _prospective_metrics_postgres(target,self.artifact('v2'),state=state)
        self.assertEqual(reset['counts']['predictions'],0)
        self.assertEqual(state['version'],'v2')
        self.assertIsNone(reset['model']['accuracy'])

    def test_settled_replay_cannot_displace_recent_pending_window(self):
        live = [self.source_row(match_id=mid) for mid in ('live-a','live-b')]
        replay = [self.source_row(predicted_at='2025-01-01T15:00:00+00:00',outcome=outcome)
                  for outcome in ('under','over')]
        final = {'m1': {'ft_score': (2,0), 'ht_score': None,
                       'first_label_available_at':'2025-01-01T14:00:00+00:00',
                       'settled_at':'2025-01-01T16:00:00+00:00','label_version':'latest'}}
        class Cursor:
            description = [(key,) for key in replay[0]]
            def execute(self, query, parameters):
                pass
            def fetchall(self):
                return [tuple(row.values()) for row in replay]
            def close(self):
                pass
        class Connection:
            def cursor(self):
                return Cursor()
        state = {'version':'candidate-v1','rows':live,'cursor':None}
        with patch('store.training_data.readonly_snapshot', return_value=nullcontext(Connection())), \
                patch('store.training_data._shadow_label_subset',return_value=final), \
                patch('store.training_data._shadow_label_events',return_value=([],False)), \
                patch('store.training_data.SHADOW_WINDOW_LIMIT',2):
            result = _prospective_metrics_postgres(ProjectionTarget.parse('postgresql://localhost/model'),
                                                   self.artifact(),state=state)
        self.assertEqual({r['match_id'] for r in state['rows']},{'live-a','live-b'})
        self.assertEqual(result['counts']['predictions'],2)
        self.assertEqual(result['counts']['pending_predictions'],2)
        self.assertEqual(result['counts']['excluded_replay_predictions'],2)
        self.assertEqual(result['counts']['excluded_replay_scope'],'cursor_history_not_window')

    def pg_cycle(self, state, batch, events, finals, persisted):
        template = self.source_row()
        class Cursor:
            description = [(key,) for key in template]
            def execute(self, query, parameters):
                pass
            def fetchall(self):
                return [tuple(row[key] for key in template) for row in batch]
            def close(self):
                pass
        class Connection:
            def cursor(self):
                return Cursor()
        def subset(connection, mids):
            return {mid:finals[mid] for mid in mids if mid in finals}
        def match(connection, model, mid):
            selected, sampled = _sample_shadow_contracts(persisted.get(mid, []))
            return {'rows':selected,'sampled':sampled,'fetched_predictions':len(persisted.get(mid, []))}
        with patch('store.training_data.readonly_snapshot',return_value=nullcontext(Connection())), \
                patch('store.training_data._shadow_label_subset',side_effect=subset), \
                patch('store.training_data._shadow_label_events',return_value=(events,False)), \
                patch('store.training_data._shadow_match_predictions',side_effect=match) as loader:
            result = _prospective_metrics_postgres(ProjectionTarget.parse('postgresql://localhost/model'),
                                                   self.artifact(),state=state)
        return result, loader

    def test_settled_label_recovers_prediction_after_pending_eviction_and_process_restart(self):
        old = [self.source_row(),self.source_row(outcome='over',probability=.2,baseline=.6)]
        newer = [{**row,'match_id':'newer'} for row in old]
        state = {}
        with patch('store.training_data.SHADOW_WINDOW_LIMIT',2):
            self.pg_cycle(state,old,[],{}, {})
            self.pg_cycle(state,newer,[],{}, {})
        self.assertEqual({r['match_id'] for r in state['rows']},{'newer'})
        event = {'match_id':'m1','revision_id':'final-v1','available_epoch':1735740000.}
        final = {'ft_score':(1,0),'ht_score':None,'settled_at':'2025-01-01T14:00:00+00:00',
                 'first_label_available_at':'2025-01-01T14:00:00+00:00','label_version':'final-v1'}
        recovered, loader = self.pg_cycle(state,[],[event],{'m1':final},{'m1':old})
        self.assertEqual(loader.call_args.args[2],'m1')
        self.assertEqual(recovered['model']['asian_payment_accuracy'],1)
        self.assertEqual(recovered['windows']['settled']['predictions'],2)
        self.assertEqual(recovered['windows']['pending']['predictions'],2)
        self.assertFalse(recovered['sampling']['full_population'])
        # JSON state survives a worker restart. Changing current raw pending
        # rows cannot erase the independently stored recent settled match.
        restored = json.loads(json.dumps(state))
        refreshed, _ = self.pg_cycle(restored,newer,[],{'m1':final},{'m1':old})
        self.assertEqual(refreshed['model']['asian_payment_accuracy'],1)
        self.assertEqual(refreshed['counts']['settled_predictions'],2)

    def test_label_batch_only_advances_through_processed_matches_and_revisions_replace(self):
        events = [{'match_id':f'm{i}','revision_id':f'r{i}','available_epoch':1735740000.+i} for i in range(5)]
        finals = {f'm{i}':{'ft_score':(1,0),'ht_score':None,'settled_at':'2025-01-01T14:00:00+00:00',
                            'first_label_available_at':'2025-01-01T14:00:00+00:00','label_version':f'r{i}'}
                  for i in range(5)}
        persisted = {f'm{i}':[self.source_row(match_id=f'm{i}'),
            self.source_row(match_id=f'm{i}',outcome='over',probability=.2,baseline=.6)] for i in range(5)}
        state = {}
        first, loader = self.pg_cycle(state,[],events,finals,persisted)
        self.assertEqual(loader.call_count,4)
        self.assertEqual(state['label_cursor'],[1735740003.,'r3'])
        self.assertTrue(first['cursor']['label_has_more'])
        second, _ = self.pg_cycle(state,[],events[4:],finals,persisted)
        self.assertEqual(second['windows']['settled']['matches'],5)
        revised = {**finals['m1'],'ft_score':(2,0),'label_version':'correction'}
        final_update = {**finals,'m1':revised}
        revision = {'match_id':'m1','revision_id':'correction','available_epoch':1735740100.}
        third, _ = self.pg_cycle(state,[],[revision],final_update,persisted)
        self.assertEqual(third['windows']['settled']['matches'],5)
        self.assertEqual(third['counts']['settled_predictions'],10)
        self.assertEqual(third['labels']['m1']['version'],'correction')
        self.assertAlmostEqual(third['model']['asian_payment_accuracy'],.8)

    def test_delayed_prediction_after_label_cursor_advanced_is_recovered_and_sampling_is_by_contract(self):
        final = {'ft_score':(1,0),'ht_score':None,'settled_at':'2025-01-01T14:00:00+00:00',
                 'first_label_available_at':'2025-01-01T14:00:00+00:00','label_version':'result'}
        event = {'match_id':'m1','revision_id':'result','available_epoch':1735740000.}
        state = {}
        self.pg_cycle(state,[],[event],{'m1':final},{})
        self.assertFalse(state['settled_buckets'])
        self.assertIn('m1',state['awaiting_prediction_matches'])
        rows = [self.source_row(),self.source_row(outcome='over',probability=.2,baseline=.6)]
        # No prediction-cursor row is needed: independent retry finds a late
        # database commit even after the result cursor has moved on.
        retry_state = json.loads(json.dumps(state))
        retry,_ = self.pg_cycle(retry_state,[],[],{'m1':final},{'m1':rows})
        self.assertEqual(retry['model']['asian_payment_accuracy'],1)
        self.assertFalse(retry_state['awaiting_prediction_matches'])
        # Simulate an incremental cursor splitting directions across batches.
        self.pg_cycle(state,rows[:1],[],{'m1':final},{'m1':rows})
        self.assertIn('m1',state['settled_buckets'])
        actual,_ = self.pg_cycle(state,rows[1:],[],{'m1':final},{'m1':rows})
        self.assertEqual(actual['model']['asian_payment_accuracy'],1)
        self.assertEqual(actual['counts']['settled_predictions'],2)
        many = [self.source_row(at=f'2025-01-01T12:{i//60:02d}:{i%60:02d}+00:00',outcome=outcome)
                for i in range(130) for outcome in ('under','over')]
        selected,sampled = _sample_shadow_contracts(many)
        self.assertTrue(sampled)
        self.assertEqual(len(selected),256)
        self.assertEqual(len({r['at'] for r in selected}),128)
        self.assertEqual({r['outcome'] for r in selected},{'under','over'})

    def test_awaiting_capacity_does_not_starve_label_cursor_and_failed_queries_do_not_advance(self):
        events = [{'match_id':f'm{i}','revision_id':f'r{i}','available_epoch':1735740000.+i} for i in range(2)]
        state = {}
        with patch('store.training_data.SHADOW_AWAITING_MATCH_LIMIT',1):
            first,_ = self.pg_cycle(state,[],events,{}, {})
        self.assertEqual(state['label_cursor'],[1735740001.,'r1'])
        self.assertFalse(first['cursor']['label_has_more'])
        self.assertEqual(len(state['awaiting_prediction_matches']),1)
        self.assertEqual(first['counts']['awaiting_prediction_matches_evicted'],1)
        frozen = json.loads(json.dumps(state))
        with patch('store.training_data._shadow_match_predictions',side_effect=TimeoutError('timeout')):
            with self.assertRaises(TimeoutError):
                # pg_cycle mocks this helper itself; run the actual function
                # with only its connection/new-label helpers replaced.
                class Cursor:
                    description = []
                    def execute(self, query, parameters):
                        pass
                    def fetchall(self):
                        return []
                    def close(self):
                        pass
                class Connection:
                    def cursor(self):
                        return Cursor()
                with patch('store.training_data.readonly_snapshot',return_value=nullcontext(Connection())), \
                        patch('store.training_data._shadow_label_events',return_value=(events[1:],False)):
                    _prospective_metrics_postgres(ProjectionTarget.parse('postgresql://localhost/model'),
                                                   self.artifact(),state=state)
        self.assertEqual(state,frozen)

    def test_label_events_query_is_indexable_bounded_and_starts_at_candidate_availability(self):
        statements = []
        class Cursor:
            description = [('match_id',),('revision_id',),('available_epoch',)]
            def execute(self, query, parameters):
                statements.append((query,parameters))
            def fetchone(self):
                return ['2025-01-01T14:00:00+00:00']
            def fetchall(self):
                return [('m1','revision',1735740000.)]
            def close(self):
                pass
        class Connection:
            def cursor(self):
                return Cursor()
        events,more = _shadow_label_events(Connection(),self.artifact(),None)
        self.assertFalse(more)
        self.assertEqual(events[0]['revision_id'],'revision')
        first_sql,_ = statements[0]
        self.assertIn('ORDER BY predicted_at,at,match_id,algorithm,market,line,outcome LIMIT 1',first_sql)
        sql,args = statements[1]
        self.assertEqual(sql.count('LIMIT %s'),3)
        self.assertIn('shadow_label_available_epoch(available_at),revision_id) > (%s,%s)',sql)
        self.assertNotIn('model_inputs',sql)
        self.assertNotIn('model_predictions',sql)
        # Candidate existed at 10:00, collection only began four hours later.
        self.assertEqual(args[:3],(1735740000.,'',64))

    def test_many_unrelated_labels_do_not_starve_new_forecasts_and_no_forecasts_skip_global_scan(self):
        events = [{'match_id':f'unrelated-{i:03d}','revision_id':f'r{i:03d}','available_epoch':1735740000.+i}
                  for i in range(70)]
        events.append({'match_id':'winner','revision_id':'winner-result','available_epoch':1735741000.})
        final = {'ft_score':(1,0),'ht_score':None,'settled_at':'2025-01-01T14:30:00+00:00',
                 'first_label_available_at':'2025-01-01T14:30:00+00:00','label_version':'winner-result'}
        winning_rows = [self.source_row(match_id='winner'),
                        self.source_row(match_id='winner',outcome='over',probability=.2,baseline=.6)]
        state = {}
        while events:
            result,_ = self.pg_cycle(state,[],events,{'winner':final},{'winner':winning_rows})
            events = events[result['cursor']['label_records']:]
        self.assertEqual(result['model']['asian_payment_accuracy'],1)
        self.assertEqual(result['windows']['settled']['matches'],1)
        self.assertLessEqual(result['counts']['awaiting_prediction_matches'],64)
        self.assertGreater(result['counts']['awaiting_prediction_matches_evicted'],0)
        self.assertEqual(state['label_cursor'],[1735741000.,'winner-result'])
        queries = []
        class Cursor:
            def execute(self, query, parameters):
                queries.append(query)
            def fetchone(self):
                return None
            def close(self):
                pass
        class Connection:
            def cursor(self):
                return Cursor()
        empty,more = _shadow_label_events(Connection(),self.artifact(),None)
        self.assertFalse(empty)
        self.assertFalse(more)
        self.assertEqual(len(queries),1)
        self.assertIn('model_predictions',queries[0])

    def test_dashboard_archive_status_merges_totals_and_exposes_freshness(self):
        from service.data_model import DataModelService
        with tempfile.TemporaryDirectory(dir=Path.cwd()/'output', prefix='shadow-dashboard-') as directory:
            root = Path(directory)
            atomic_json(root/'models'/'dashboard.json', {'service':'model-worker','running':True,
                'heartbeat_at':datetime.now(timezone.utc).isoformat(), 'training':{},
                'storage':{'at':datetime.now(timezone.utc).isoformat(),
                    'archive':{'backend':'s3','files':20,'bytes':123,'object_bytes':456}}})
            atomic_json(root/'_archive'/'status.json', {'running':True,'phase':'idle'})
            dashboard = DataModelService()
            dashboard.bind(str(root/'ledger'))
            result = dashboard.status()
            self.assertEqual(result['storage']['archive']['files'],20)
            self.assertEqual(result['storage']['archive']['backend'],'s3')
            self.assertEqual(result['storage']['archive']['phase'],'idle')
            self.assertEqual(result['storage']['summary_status'],'ready')
            json.dumps(result,allow_nan=False)
