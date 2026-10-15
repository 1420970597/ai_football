"""Shared watermarks gate optional jobs without touching collection/history."""
from collections import namedtuple
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from service.archive_service import backup_databases
from service.model_service import ModelService
from service.model_training import run
from service.runtime_settings import RuntimeConfig
from store.disk_pressure import GIB, disk_pressure

Usage = namedtuple('Usage', 'total used free')


class DiskPressureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def sample(self, free):
        with patch('store.disk_pressure.shutil.disk_usage', return_value=Usage(100*GIB, (100-free)*GIB, free*GIB)):
            return disk_pressure(self.root)

    def expire(self):
        path = self.root/'_archive'/'disk-pressure.json'
        state = json.loads(path.read_text())
        state['sampled_at'] = 0
        path.write_text(json.dumps(state))

    def test_shared_hysteresis_and_recovery(self):
        self.assertEqual(self.sample(30)['state'], 'normal')
        self.expire()
        self.assertEqual(self.sample(15)['state'], 'warning')
        self.expire()
        self.assertEqual(self.sample(8)['state'], 'critical')
        self.expire()
        self.assertTrue(self.sample(9)['pause_optional_writes'])
        self.expire()
        self.assertFalse(self.sample(10)['pause_optional_writes'])

    def test_api_stale_read_fails_closed_and_never_publishes(self):
        self.assertEqual(disk_pressure(self.root, publish=False)['state'], 'unknown')
        self.assertFalse((self.root/'_archive').exists())
        self.sample(30)
        self.assertEqual(disk_pressure(self.root, publish=False)['state'], 'normal')
        self.expire()
        self.assertEqual(disk_pressure(self.root, publish=False)['state'], 'unknown')

    def test_invalid_configuration_and_sampling_failure_pause_optional_work(self):
        with patch.dict(os.environ, {'DISK_CRITICAL_FREE_RATIO':'nan'}):
            self.assertTrue(disk_pressure(self.root)['pause_optional_writes'])
        with patch('store.disk_pressure.shutil.disk_usage', side_effect=OSError('unavailable')):
            self.assertEqual(disk_pressure(self.root)['state'], 'unknown')

    def test_critical_state_prevents_new_training_and_backups_then_recovers(self):
        worker = ModelService()
        cfg = RuntimeConfig(model_training_enabled=True)
        with patch('service.model_service.disk_pressure', return_value={
                'pause_optional_writes':True, 'reason':'critical'}), \
             patch('service.model_service.subprocess.Popen') as child:
            worker._start_training_check(self.root/'ledger', cfg)
            child.assert_not_called()
            self.assertEqual(worker._status['state'], 'waiting_for_disk')
            self.assertTrue(cfg.model_training_enabled)
        with patch('service.model_service.disk_pressure', return_value={'pause_optional_writes':False}), \
             patch('service.model_service.subprocess.Popen') as child:
            worker._start_training_check(self.root/'ledger', cfg)
            child.assert_called_once()
        with patch('service.archive_service.disk_pressure', return_value={'pause_optional_writes':True}):
            self.assertEqual(backup_databases(self.root), [])
        with patch('service.model_training.constrain', return_value={}), \
             patch('service.model_training.disk_pressure', return_value={'pause_optional_writes':True,'reason':'critical'}), \
             patch('service.model_training.training_cohort_progress') as cohort, \
             patch('service.model_training.dataset') as dataset:
            self.assertEqual(run(self.root/'ledger',100,1,256)['state'],'waiting_for_disk')
            cohort.assert_not_called()
            dataset.assert_not_called()


if __name__ == '__main__':
    unittest.main()
