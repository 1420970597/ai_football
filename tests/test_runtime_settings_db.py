"""PG settings are authoritative, versioned and fail closed on startup."""
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from service.runtime_settings import RuntimeSettings, VersionConflict
from store.runtime_settings_db import load_runtime_settings, save_runtime_settings


class SettingsPersistenceTests(unittest.TestCase):
    def test_new_installation_can_seed_startup_switch(self):
        with tempfile.TemporaryDirectory() as root, \
             patch('service.runtime_settings.postgres_enabled', return_value=True), \
             patch('service.runtime_settings.load_runtime_settings', side_effect=lambda seed: seed), \
             patch.dict('os.environ', {'AUTO_ENABLE_BETTING': '1'}):
            settings = RuntimeSettings()
            settings.bind(root + '/ledger')
            self.assertTrue(settings.config.betting_enabled)

    def test_pg_precedes_local_and_restart_preserves_saved_switch(self):
        with tempfile.TemporaryDirectory() as root:
            path = root + '/runtime-settings.json'
            with open(path, 'w') as stream:
                json.dump({'version': 3, 'settings': {'min_ev': .2}}, stream)
            document = {'version': 9, 'settings': {'min_ev': .04, 'betting_enabled': False}}
            with patch('service.runtime_settings.postgres_enabled', return_value=True), \
                 patch('service.runtime_settings.load_runtime_settings', return_value=document) as load, \
                 patch.dict('os.environ', {'AUTO_ENABLE_BETTING': '1'}):
                settings = RuntimeSettings()
                settings.bind(root + '/ledger')
                self.assertEqual(settings.version, 9)
                self.assertEqual(settings.config.min_ev, .04)
                self.assertFalse(settings.config.betting_enabled)
                self.assertEqual(load.call_args.args[0]['version'], 3)
                self.assertEqual(settings.public()['persistence_backend'], 'postgres')
                restored = RuntimeSettings()
                restored.bind(root + '/ledger')
                self.assertEqual(restored.snapshot(), settings.snapshot())

    def test_unavailable_pg_never_falls_back_and_failed_save_never_applies(self):
        with tempfile.TemporaryDirectory() as root, \
             patch('service.runtime_settings.postgres_enabled', return_value=True), \
             patch('service.runtime_settings.load_runtime_settings', side_effect=OSError('offline')):
            with self.assertRaisesRegex(ValueError, '未回退'):
                RuntimeSettings().bind(root + '/ledger')
        settings = RuntimeSettings()
        settings.persistence_backend = 'postgres'
        callback = MagicMock()
        with patch('service.runtime_settings.save_runtime_settings', side_effect=OSError('offline')):
            with self.assertRaises(ValueError):
                settings.update({'min_ev': .3}, 1, callback)
        callback.assert_not_called()
        self.assertEqual(settings.config.min_ev, .02)
        self.assertEqual(settings.version, 1)

    def test_cas_conflict_refreshes_current_configuration(self):
        settings = RuntimeSettings()
        settings.persistence_backend = 'postgres'
        callback = MagicMock()
        with patch('service.runtime_settings.save_runtime_settings', return_value=False), \
             patch('service.runtime_settings.load_runtime_settings', return_value={
                 'version': 5, 'settings': {'min_ev': .07}}):
            with self.assertRaises(VersionConflict):
                settings.update({'min_ev': .3}, 1, callback)
        self.assertEqual(settings.version, 5)
        self.assertEqual(settings.config.min_ev, .07)
        callback.assert_called_once()

    def test_successful_pg_save_commits_before_apply_and_keeps_secret_private(self):
        settings = RuntimeSettings()
        settings.persistence_backend = 'postgres'
        order = []
        def save(document, version):
            order.append('save')
            self.assertEqual(version, 1)
            self.assertEqual(document['version'], 2)
            return True
        with patch('service.runtime_settings.save_runtime_settings', side_effect=save):
            out = settings.update({'llm_api_key': 'fixture-key'}, 1,
                                  lambda *_: order.append('apply'))
        self.assertEqual(order, ['save', 'apply'])
        self.assertNotIn('fixture-key', json.dumps(out))


class SettingsDatabaseTests(unittest.TestCase):
    def test_first_initializer_wins_and_reader_does_not_create_schema(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = (8, {'min_ev': .05})
        with patch('store.runtime_settings_db.connect', return_value=db):
            self.assertEqual(load_runtime_settings()['version'], 8)
            self.assertEqual(db.execute.call_count, 1)
            db.reset_mock()
            load_runtime_settings({'version': 1, 'settings': {}})
            self.assertIn('ON CONFLICT(name) DO NOTHING', db.execute.call_args_list[1].args[0])
        db.close.assert_called()

    def test_save_uses_version_condition_and_reports_conflict(self):
        db = MagicMock()
        db.execute.return_value.rowcount = 0
        with patch('store.runtime_settings_db.connect', return_value=db):
            self.assertFalse(save_runtime_settings({'version': 3, 'settings': {}}, 2))
        self.assertIn('AND version=?', db.execute.call_args.args[0])
        self.assertEqual(db.execute.call_args.args[1][-1], 2)

    def test_invalid_database_document_is_rejected(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = (1, [])
        with patch('store.runtime_settings_db.connect', return_value=db):
            with self.assertRaises(ValueError):
                load_runtime_settings()
