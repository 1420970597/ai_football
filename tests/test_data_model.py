from dataclasses import replace
import json
from contextlib import closing
import time
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from api.app import ApiApp
from core.learned_model import FEATURES
from service.analysis import AnalysisConfig, AnalysisService
from service.data_model import DataModelService as Dashboard, storage_stats
from service.model_service import ModelService as DataModelService
from service.model_training import atomic_json
from service.runtime_settings import RuntimeConfig, validated
from store.ledger_db import LedgerDB
from store.training_data import write_inputs
from tests.test_learned_model import sample_row
from tests.test_training_data import decision


class DataModelTests(unittest.TestCase):
    def test_settings_strict_and_persisted(self):
        cfg = RuntimeConfig()
        out = validated(cfg, {'model_training_matches': 2, 'model_training_cpu': 2, 'model_training_memory_mb': 256})
        self.assertEqual(out.model_training_matches, 2)
        for patch_value in ({'model_training_matches': 0}, {'model_training_cpu': 3},
                            {'model_training_memory_mb': 100}, {'model_training_cpu': 1.5},
                            {'model_training_enabled': 1}, {'model_training_matches': True}):
            with self.subTest(patch=patch_value), self.assertRaises(ValueError):
                validated(cfg, patch_value)

    def test_get_returns_cache_without_scanning_or_starting_training(self):
        with tempfile.TemporaryDirectory() as root:
            svc = AnalysisService(MagicMock(), config=AnalysisConfig(use_llm=False, ledger_root=root+'/ledger'))
            app = ApiApp(MagicMock(), analysis=svc)
            with patch('service.data_model.storage_stats') as stats, patch('service.model_service.subprocess.Popen') as process:
                code, out = app.dispatch('GET', '/api/v1/data-model', {}, {})
                self.assertEqual(code, 200)
                self.assertEqual(out['model'], {})
                self.assertEqual(out['mode'], 'shadow')
                stats.assert_not_called()
                process.assert_not_called()

    def test_storage_counts_files_bytes_and_preserves_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'_trends').mkdir()
            (root/'_trends'/'m.jsonl').write_bytes(b'123456')
            (root/'legacy.json').write_bytes(b'1234')
            stats = storage_stats(root)
            self.assertEqual(stats['bytes'], 10)
            self.assertEqual(stats['files'], 2)
            self.assertEqual(stats['errors'], [])
            self.assertGreater(stats['disk']['total'], stats['disk']['free'])
            self.assertTrue((root/'legacy.json').exists())

    def test_observation_keeps_multiple_decisions_and_persists_without_model(self):
        with tempfile.TemporaryDirectory() as root:
            svc = DataModelService()
            svc.bind(root+'/ledger')
            svc.observe(decision())
            svc._flush_shadow()
            with closing(sqlite3.connect(Path(root)/'models'/'inputs.sqlite3')) as con:
                self.assertEqual(con.execute('SELECT count(*) FROM inputs').fetchone()[0], 8)
            self.assertFalse(svc.status()['model'])
            svc.configure(replace(RuntimeConfig(), model_training_enabled=False))
            self.assertEqual(svc.status()['training']['state'], 'disabled')

    def test_worker_actual_limits_versions_restart_and_failure_preserve_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'ledger'
            root.mkdir()
            db = LedgerDB(root/'ledger.sqlite3')
            finals, inputs = [], []
            for i in range(120):
                row = sample_row(i)
                finals.append({'match_id': str(i), 'at': row['at'], 'settled_at': row['settled_at'],
                               'competition_type': 'real','status':'won','ft_score': [2,0] if row['y'] else [0,2]})
                inputs.append({'match_id':str(i), 'at':row['at'],'cutoff_ms':1,'algorithm':'poisson_time_decay',
                               'market':'HAD','line':'','outcome':'home','baseline':row['baseline'],'x':json.dumps(row['x'])})
            db.put(finals, lambda _: 1.0)
            db.connection.commit()
            write_inputs(root.parent/'models'/'inputs.sqlite3', inputs)
            args = [sys.executable,'-m','service.model_training',str(root),'100','1','256']
            result = subprocess.run(args, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            active = root.parent/'models'/'active.json'
            model = json.loads(active.read_text())
            self.assertEqual(model['limits']['cpu_cores'], 1)
            self.assertEqual(model['limits']['memory_mb'], 256)
            self.assertEqual(model['parameter_count'], len(FEATURES))
            original = active.read_bytes()
            result = subprocess.run(args, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(active.read_bytes(), original)
            self.assertEqual(json.loads((active.parent/'status.json').read_text())['new_matches'], 0)
            # A corrupt projection fails loudly and retains the prior successful artifact.
            with closing(sqlite3.connect(db.path)) as con, con:
                con.execute('DROP TABLE decisions')
            result = subprocess.run(args, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(active.read_bytes(), original)
            self.assertEqual(json.loads((active.parent/'status.json').read_text())['state'], 'failed')

    def test_failed_input_write_keeps_retry_queue(self):
        with tempfile.TemporaryDirectory() as root:
            svc = DataModelService()
            svc.bind(root+'/ledger')
            svc.observe(decision())
            with patch('service.model_service.write_inputs', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    svc._flush_shadow()
            self.assertEqual(len(svc._inputs),8)
            svc._flush_shadow()
            self.assertEqual(len(svc._inputs),0)

    def test_bad_model_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as root:
            atomic_json(Path(root)/'models'/'active.json', {'version':'x','features':list(FEATURES),'weights':[0]})
            svc=DataModelService()
            svc.bind(root+'/ledger')
            self.assertFalse(svc.status()['model'])
            self.assertEqual(svc.status()['training']['state'],'failed')

    def test_resource_change_terminates_only_owned_worker(self):
        svc = DataModelService()
        process = MagicMock()
        process.poll.return_value = None
        svc._process = process
        svc.configure(replace(RuntimeConfig(), model_training_cpu=2))
        process.terminate.assert_called_once()
        process.reset_mock()
        svc.configure(replace(svc.cfg, model_training_enabled=False))
        process.terminate.assert_called_once()

    def test_active_training_does_not_block_input_flush(self):
        with tempfile.TemporaryDirectory() as root:
            svc = DataModelService()
            svc.bind(root+'/ledger')
            process = MagicMock()
            process.poll.return_value = None
            with patch('service.model_service.subprocess.Popen', return_value=process):
                svc.start()
                deadline = time.monotonic()+3
                while svc._process is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertIs(svc._process, process)
                svc.observe(decision())
                while svc._inputs and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(svc._inputs)
                self.assertTrue((Path(root)/'models'/'inputs.sqlite3').exists())
                svc.stop()
                self.assertGreaterEqual(process.terminate.call_count, 1)
                self.assertFalse(svc.status()['running'])

    def test_shadow_predictions_are_versioned_and_actual_metrics_use_settled_results(self):
        from core.learned_model import train
        with tempfile.TemporaryDirectory() as root:
            svc = DataModelService()
            svc.bind(root+'/ledger')
            model = train([sample_row(i) for i in range(100)])
            model.update(version='v1',updated_at='2024-12-31T00:00:00+00:00')
            atomic_json(Path(root)/'models'/'active.json',model)
            svc._read()
            with patch('service.model_service.time.time', return_value=1735732800):
                svc.observe(decision())
            svc._flush_shadow()
            path=Path(root)/'models'/'shadow.sqlite3'
            with closing(sqlite3.connect(path)) as con:
                self.assertEqual(con.execute('SELECT count(*) FROM predictions').fetchone()[0],8)
            svc.observe(decision())
            svc._flush_shadow()
            with closing(sqlite3.connect(path)) as con:
                self.assertEqual(con.execute('SELECT count(*) FROM predictions').fetchone()[0],8)
            svc.root.mkdir()
            db=LedgerDB(svc.root/'ledger.sqlite3')
            db.put([{'match_id':'m1','at':decision()['computed_at'],'settled_at':'2025-01-01T14:00:00+00:00',
                     'competition_type':'real','status':'won','ft_score':[2,1],'ht_score':[1,0]}],lambda _:1.)
            db.connection.commit()
            from store.training_data import prospective_metrics
            evaluation=prospective_metrics(db.path,path,model)
            self.assertEqual(evaluation['model']['matches'],1)
            self.assertEqual(evaluation['model']['decisions'],8)
            self.assertIn('baseline',evaluation)
            with closing(sqlite3.connect(path)) as con, con:
                con.execute("UPDATE predictions SET predicted_at='2025-01-01T15:00:00+00:00'")
            self.assertEqual(prospective_metrics(db.path,path,model),{})
            atomic_json(path.parent/'prospective.json',evaluation)
            svc._read()
            self.assertEqual(svc.status()['prospective']['version'],'v1')
            model['version']='v2'
            atomic_json(path.parent/'active.json',model)
            svc._read()
            self.assertFalse(svc.status()['prospective'])

    def test_dashboard_reads_worker_completion_without_starting_any_worker(self):
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as root:
            svc = Dashboard()
            svc.bind(root+'/ledger')
            self.assertFalse(svc.status()['running'])
            atomic_json(Path(root)/'models'/'dashboard.json', {
                'service':'model-worker','running':True,'heartbeat_at':datetime.now(timezone.utc).isoformat(),
                'training':{'state':'ready'},'model':{'version':'v1'},'storage':{}})
            state = svc.status()
            self.assertTrue(state['running'])
            self.assertEqual(state['model']['version'],'v1')
            self.assertEqual(state['training']['state'],'ready')
            atomic_json(Path(root)/'models'/'dashboard.json', {
                'service':'model-worker','running':True,'heartbeat_at':'2020-01-01T00:00:00+00:00',
                'training':{'state':'training'},'model':{'version':'v1'},'storage':{}})
            self.assertFalse(svc.status()['running'])
            self.assertEqual(svc.status()['model']['version'],'v1')
