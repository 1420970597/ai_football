import sqlite3
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from store.training_projection import (
    ProjectionConfigError,
    ProjectionDependencyError,
    ProjectionTarget,
    fetch_batches,
    ensure_postgres_schema,
    prepare_postgres_schema,
    readonly_connection,
    readonly_snapshot,
)


class TrainingProjectionTests(unittest.TestCase):
    def test_live_writer_reuses_thread_connection_and_reconnects_after_failed_commit(self):
        import store.training_projection as projection
        events = []
        class Cursor:
            def execute(self,*args):
                events.append('configure')
            def close(self):
                pass
        class Connection:
            fail = False
            def cursor(self):
                return Cursor()
            def commit(self):
                events.append('commit')
                if self.fail:
                    raise TimeoutError('confirmation lost')
            def rollback(self):
                events.append('rollback')
            def close(self):
                events.append('close')
        first,second = Connection(),Connection()
        target = ProjectionTarget.parse('postgresql://live-writer-test/model')
        with patch.object(projection,'_PERSISTENT_WRITER',types.SimpleNamespace()), \
                patch.object(projection,'_connect_postgres',side_effect=[first,second]) as connect:
            with projection.persistent_writer(target) as con:
                self.assertIs(con,first)
            with projection.persistent_writer(target) as con:
                self.assertIs(con,first)
            first.fail = True
            with self.assertRaises(TimeoutError):
                with projection.persistent_writer(target):
                    pass
            with projection.persistent_writer(target) as con:
                self.assertIs(con,second)
            projection.close_persistent_writer()
            self.assertEqual(connect.call_count,2)
        self.assertEqual(events.count('configure'),2)
        self.assertEqual(events.count('rollback'),1)
        self.assertEqual(events.count('close'),2)

    def test_schema_preparation_commits_once_and_failed_setup_remains_retryable(self):
        from contextlib import contextmanager
        import store.training_projection as projection
        events = []
        @contextmanager
        def writer(target):
            events.append('begin')
            yield object()
            events.append('commit')
        target = ProjectionTarget.parse('postgresql://schema-cache-test/model')
        with patch.object(projection,'_PREPARED_SCHEMA_LOCATIONS',set()), \
                patch.object(projection,'writable_connection',side_effect=writer), \
                patch.object(projection,'ensure_postgres_schema',side_effect=[TimeoutError('DDL'),None]) as ddl:
            with self.assertRaises(TimeoutError):
                prepare_postgres_schema(target)
            prepare_postgres_schema(target)
            prepare_postgres_schema(target)
        self.assertEqual(ddl.call_count,2)
        self.assertEqual(events,['begin','begin','commit'])

    def test_postgres_schema_setup_is_serialized_and_reuses_migration_index(self):
        statements = []

        class Cursor:
            def execute(self, operation, parameters=()):
                statements.append(operation)

            def close(self):
                pass

        class Connection:
            def cursor(self):
                return Cursor()

        ensure_postgres_schema(Connection())
        self.assertIn("SELECT pg_advisory_xact_lock(1984, 4201)", statements)
        self.assertTrue(any("CREATE INDEX IF NOT EXISTS idx_migrate_model_inputs" in item
                            for item in statements))
        self.assertFalse(any("idx_model_inputs_training" in item for item in statements))

    def test_parse_local_sqlite_path_and_uri(self):
        path = ProjectionTarget.parse("/tmp/model.sqlite3")
        self.assertEqual(path.backend, "sqlite")
        self.assertEqual(path.location, "/tmp/model.sqlite3")
        uri = ProjectionTarget.parse("sqlite:///tmp/model.sqlite3")
        self.assertEqual(uri.location, "/tmp/model.sqlite3")

    def test_parse_postgres_redacts_password(self):
        target = ProjectionTarget.parse("postgresql://alice:secret@example.test/model")
        self.assertEqual(target.backend, "postgresql")
        self.assertNotIn("secret", repr(target))
        self.assertIn("alice:***@example.test", target.redacted_location())

    def test_rejects_invalid_target_settings(self):
        with self.assertRaises(ProjectionConfigError):
            ProjectionTarget.parse("mysql://localhost/model")
        with self.assertRaises(ProjectionConfigError):
            ProjectionTarget("sqlite", "/tmp/model.sqlite3", fetch_size=0)

    def test_postgres_backend_with_missing_credentials_does_not_fallback_to_sqlite(self):
        import store.training_projection as projection
        with patch.dict('os.environ', {'DATABASE_BACKEND': 'postgres'}, clear=True):
            with self.assertRaises(ProjectionConfigError):
                projection.projection_target_from_env()

    def test_sqlite_persistent_database_is_read_only_but_temp_tables_work(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "projection.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE rows (value INTEGER)")
                connection.execute("INSERT INTO rows VALUES (1),(2),(3)")
            target = ProjectionTarget.parse(path)
            with readonly_connection(target) as connection:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("INSERT INTO rows VALUES (4)")
                connection.execute("CREATE TEMP TABLE labels (value INTEGER)")
                connection.execute("INSERT INTO labels VALUES (10)")
                values = connection.execute("SELECT value FROM rows ORDER BY value").fetchall()
                self.assertEqual([row[0] for row in values], [1, 2, 3])

    def test_snapshot_rolls_back_and_fetches_in_bounded_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "projection.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE rows (value INTEGER)")
                connection.executemany("INSERT INTO rows VALUES (?)", [(i,) for i in range(5)])
            target = ProjectionTarget.parse(path)
            with readonly_snapshot(target) as connection:
                cursor = connection.execute("SELECT value FROM rows ORDER BY value")
                batches = list(fetch_batches(cursor, "SELECT value FROM rows ORDER BY value", batch_size=2))
                self.assertEqual([[row[0] for row in batch] for batch in batches], [[0, 1], [2, 3], [4]])

    def test_postgres_driver_configuration_and_snapshot_are_read_only(self):
        calls = []

        class Cursor:
            def execute(self, operation, parameters=()):
                calls.append((operation, parameters))

            def close(self):
                calls.append(("close", ()))

        class Connection:
            def cursor(self):
                return Cursor()

            def commit(self):
                calls.append(("commit", ()))

            def rollback(self):
                calls.append(("rollback", ()))

            def close(self):
                calls.append(("connection-close", ()))

        module = types.SimpleNamespace(connect=lambda *args, **kwargs: (calls.append((args, kwargs)) or Connection()))
        target = ProjectionTarget.parse("postgresql://localhost/model")
        with readonly_snapshot(target, driver=module):
            pass
        operations = [call[0] for call in calls if isinstance(call[0], str)]
        self.assertIn("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY", operations)
        self.assertIn("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY", operations)
        self.assertIn("rollback", operations)

    def test_missing_postgres_driver_is_actionable(self):
        target = ProjectionTarget.parse("postgresql://localhost/model")
        # An injected driver keeps this test independent of the host package set;
        # the public error is covered by the loader's contract in production.
        import store.training_projection as projection
        original = projection._load_postgres_driver
        projection._load_postgres_driver = lambda: (_ for _ in ()).throw(ProjectionDependencyError("missing"))
        try:
            with self.assertRaises(ProjectionDependencyError):
                with readonly_connection(target):
                    pass
        finally:
            projection._load_postgres_driver = original

    def test_dataset_uses_postgres_projection_without_touching_local_paths(self):
        import store.training_data as training_data

        sentinel = [{'match_id': 'pg-match'}]
        with patch.dict('os.environ', {'TRAINING_PROJECTION_DSN': 'postgresql://localhost/model'}), \
                patch.object(training_data, '_dataset_postgres', return_value=sentinel) as reader:
            result = training_data.dataset(Path('/does/not/exist/ledger.sqlite3'),
                                           Path('/does/not/exist/inputs.sqlite3'))
        self.assertIs(result, sentinel)
        reader.assert_called_once()

    def test_prospective_metrics_uses_postgres_projection_without_local_files(self):
        import store.training_data as training_data

        model = {'version': 'v'}
        sentinel = {'version': 'v', 'model': {}, 'baseline': {}}
        with patch.dict('os.environ', {'TRAINING_PROJECTION_DSN': 'postgresql://localhost/model'}), \
                patch.object(training_data, '_prospective_metrics_postgres', return_value=sentinel) as reader:
            result = training_data.prospective_metrics(Path('/missing/ledger.sqlite3'),
                                                       Path('/missing/shadow.sqlite3'), model)
        self.assertIs(result, sentinel)
        reader.assert_called_once()

    def test_postgres_input_upsert_uses_projection_table(self):
        import store.training_data as training_data
        calls = []

        class Cursor:
            def execute(self, operation, parameters=()):
                calls.append((operation, parameters))

            def executemany(self, operation, parameters):
                calls.append((operation, list(parameters)))

            def close(self):
                calls.append(('close', ()))

        class Connection:
            def cursor(self):
                return Cursor()

            def commit(self):
                calls.append(('commit', ()))

            def rollback(self):
                calls.append(('rollback', ()))

            def close(self):
                calls.append(('connection-close', ()))

        module = types.SimpleNamespace(connect=lambda *args, **kwargs: Connection())
        row = {
            'match_id': 'm1', 'at': '2025-01-01T00:00:00+00:00', 'cutoff_ms': 1000,
            'algorithm': 'a', 'market': 'OU', 'line': '2.5', 'outcome': 'over',
            'baseline': .5, 'x': '[0]', 'decision_received_at_ms': 900,
            'quote_received_at_ms': 800, 'entry_score': '[0,0]',
            'settlement_basis': 'full_score', 'model_version': 'm',
            'data_version': 'd', 'feature_version': 'f',
        }
        target = ProjectionTarget.parse('postgresql://localhost/model')
        import store.training_projection as projection
        with patch.object(projection, '_load_postgres_driver', return_value=module):
            training_data.write_inputs(Path('/unused.sqlite3'), [row], target=target)
        statements = [item[0] for item in calls if isinstance(item[0], str)]
        self.assertTrue(any('CREATE TABLE IF NOT EXISTS model_inputs' in item for item in statements))
        self.assertTrue(any('INSERT INTO model_inputs' in item and 'ON CONFLICT' in item for item in statements))


if __name__ == "__main__":
    unittest.main()
