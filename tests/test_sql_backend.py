from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from store.postgres import PGConnection, PGResult, _postgres_params, _translate
from store.sql_backend import (POSTGRES, SQLITE, PostgresAdapter, SQLiteAdapter,
                               StorageBackendError, _replace_markers,
                               backend_from_url)


class _FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.cursors: list[_FakeCursor] = []
        self.fail_execute = False
        self.autocommit = False

    def cursor(self):
        cursor = _FakeCursor([], connection=self)
        self.cursors.append(cursor)
        return cursor

    def execute(self, sql: str, params: tuple = ()):
        cursor = self.cursor()
        cursor.execute(sql, params)
        return cursor

    def executemany(self, sql: str, params: list[tuple]) -> "_FakeConnection":
        self.calls.append((sql, tuple(params)))
        return self

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _FakeCursor:
    description = (("value",),)
    rowcount = 1

    def __init__(self, rows, connection=None):
        self.rows = list(rows)
        self.closed = False
        self.connection = connection

    def execute(self, sql, params=()):
        self.connection.calls.append((sql, tuple(params)))
        if self.connection.fail_execute:
            raise RuntimeError('query failed')
        self.description = (("value",),) if sql.upper().startswith('SELECT') else None
        self.rows = [(params[0],)] if self.description and params else []

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        self.closed = True


class SQLBackendTests(unittest.TestCase):
    @staticmethod
    def _pg_wrapper(connection):
        wrapper = object.__new__(PGConnection)
        wrapper._connection = connection
        wrapper._explicit_transaction = False
        return wrapper

    def test_postgres_context_commits_multiple_statements_only_at_exit(self):
        connection = _FakeConnection()
        wrapper = self._pg_wrapper(connection)
        with wrapper:
            wrapper.execute('UPDATE records SET value=?', (1,)).close()
            wrapper.execute('SELECT ?', (2,)).fetchone()
            self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.commits, 1)
        self.assertFalse(wrapper._explicit_transaction)
        self.assertTrue(all(cursor.closed for cursor in connection.cursors))

    def test_postgres_context_exception_rolls_back_without_early_commit(self):
        connection = _FakeConnection()
        wrapper = self._pg_wrapper(connection)
        with self.assertRaisesRegex(RuntimeError, 'abort'):
            with wrapper:
                wrapper.execute('UPDATE records SET value=?', (1,)).close()
                raise RuntimeError('abort')
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertFalse(wrapper._explicit_transaction)

    def test_postgres_implicit_result_closes_cursor_and_commits(self):
        connection = _FakeConnection()
        wrapper = self._pg_wrapper(connection)
        self.assertEqual(wrapper.execute('SELECT ?', (3,)).fetchone()[0], 3)
        self.assertEqual(connection.commits, 1)
        self.assertTrue(connection.cursors[-1].closed)

    def test_postgres_execute_error_recovers_implicit_connection(self):
        connection = _FakeConnection()
        connection.fail_execute = True
        wrapper = self._pg_wrapper(connection)
        with self.assertRaisesRegex(RuntimeError, 'query failed'):
            wrapper.execute('SELECT ?', (3,))
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.cursors[-1].closed)
        connection.fail_execute = False
        self.assertEqual(wrapper.execute('SELECT ?', (4,)).fetchone()[0], 4)

    def test_postgres_explicit_execute_error_defers_rollback_to_context(self):
        connection = _FakeConnection()
        wrapper = self._pg_wrapper(connection)
        with self.assertRaisesRegex(RuntimeError, 'query failed'):
            with wrapper:
                connection.fail_execute = True
                try:
                    wrapper.execute('SELECT ?', (3,))
                except RuntimeError:
                    self.assertEqual(connection.rollbacks, 0)
                    self.assertTrue(connection.cursors[-1].closed)
                    raise
        self.assertEqual(connection.rollbacks, 1)

    def test_sqlite_bootstrap_retries_only_transient_journal_lock(self):
        locked = sqlite3.OperationalError('database is locked')
        locked.sqlite_errorcode = sqlite3.SQLITE_BUSY
        connection = MagicMock()
        connection.execute.side_effect = [MagicMock(), locked, MagicMock(), MagicMock()]
        with patch('store.sql_backend.sqlite3.connect', return_value=connection), \
                patch('store.sql_backend.time.sleep') as sleep:
            adapter = SQLiteAdapter.open(Path('unused.sqlite3'))
        self.assertEqual(connection.execute.call_count, 4)
        sleep.assert_called_once()
        adapter.close()

    def test_sqlite_bootstrap_closes_connection_for_non_lock_error(self):
        connection = MagicMock()
        connection.execute.side_effect = [MagicMock(), sqlite3.OperationalError('disk I/O error')]
        with patch('store.sql_backend.sqlite3.connect', return_value=connection), \
                patch('store.sql_backend.time.sleep') as sleep:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'disk I/O error'):
                SQLiteAdapter.open(Path('unused.sqlite3'))
        connection.close.assert_called_once()
        sleep.assert_not_called()

    def test_postgres_marker_translation_preserves_literal_percent_labels(self):
        self.assertEqual(_translate("SELECT '80–100%' WHERE value=?", (1,)),
                         "SELECT '80–100%%' WHERE value=%s")
        self.assertEqual(_translate("SELECT '60–80%' WHERE value=:value", {'value': 1}),
                         "SELECT '60–80%%' WHERE value=%(value)s")

    def test_postgres_parameters_normalize_legacy_boolean_flags(self):
        self.assertEqual(_postgres_params((True, False, 3)), (1, 0, 3))
        self.assertEqual(_postgres_params({'is_pick': True, 'value': 2}),
                         {'is_pick': 1, 'value': 2})

    def test_postgres_result_closes_cursor_after_fetch(self):
        cursor = _FakeCursor([(7,)])
        result = PGResult(cursor)

        self.assertEqual(result.fetchone()[0], 7)
        self.assertTrue(cursor.closed)

    def test_postgres_connection_rolls_back_before_close(self):
        connection = type("Connection", (), {
            "closed": False,
            "rollback": lambda self: setattr(self, "rolled_back", True),
            "close": lambda self: setattr(self, "closed", True),
        })()
        wrapper = object.__new__(PGConnection)
        wrapper._connection = connection

        wrapper.close()

        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)

    def test_marker_translation_does_not_change_literals_or_comments(self) -> None:
        sql = "SELECT '?' AS literal, ? AS value -- ?\n /* ? */ AND x='it''s ?'"
        self.assertEqual(
            _replace_markers(sql, "%s"),
            "SELECT '?' AS literal, %s AS value -- ?\n /* ? */ AND x='it''s ?'",
        )

    def test_dialect_sql_shapes_match_each_engine(self) -> None:
        self.assertEqual(SQLITE.begin(write=True), "BEGIN IMMEDIATE")
        self.assertEqual(POSTGRES.begin(write=True), "BEGIN")
        self.assertEqual(SQLITE.placeholders(3), "?,?,?")
        self.assertEqual(POSTGRES.placeholders(3), "%s,%s,%s")
        self.assertEqual(SQLITE.json_text("payload", "status"), "json_extract(payload, '$.status')")
        self.assertEqual(POSTGRES.json_text("payload", "status"), "(payload::jsonb ->> 'status')")
        self.assertIn("INSERT OR IGNORE", SQLITE.insert_ignore("labels", ("id", "status")))
        self.assertIn("ON CONFLICT DO NOTHING", POSTGRES.insert_ignore("labels", ("id", "status")))
        with self.assertRaises(ValueError):
            POSTGRES.json_text("payload;drop", "status")

    def test_sqlite_adapter_transaction_commits_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            adapter = SQLiteAdapter.open(Path(root) / "store.sqlite3")
            adapter.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
            with adapter.transaction(write=True):
                adapter.execute("INSERT INTO records VALUES (?, ?)", (1, "ok"))
            self.assertEqual(adapter.execute("SELECT value FROM records").fetchone()[0], "ok")
            with self.assertRaisesRegex(ValueError, "abort"):
                with adapter.transaction(write=True):
                    adapter.execute("INSERT INTO records VALUES (?, ?)", (2, "rolled-back"))
                    raise ValueError("abort")
            self.assertIsNone(adapter.execute("SELECT 1 FROM records WHERE id=2").fetchone())
            self.assertEqual(adapter.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            adapter.close()

    def test_conflict_ignore_keeps_order_claim_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            adapter = SQLiteAdapter.open(Path(root) / "orders.sqlite3")
            adapter.execute("CREATE TABLE orders (identity TEXT PRIMARY KEY, payload TEXT)")
            claim = SQLITE.insert_ignore("orders", ("identity", "payload"))
            with adapter.transaction(write=True):
                adapter.execute(claim, ("same", "first"))
            with adapter.transaction(write=True):
                cursor = adapter.execute(claim, ("same", "second"))
            self.assertEqual(cursor.rowcount, 0)
            self.assertEqual(adapter.execute("SELECT payload FROM orders WHERE identity=?", ("same",)).fetchone()[0], "first")
            adapter.close()

    def test_postgres_adapter_uses_markers_and_short_transactions_without_network(self) -> None:
        fake = _FakeConnection()
        adapter = PostgresAdapter.connect("postgresql://unit-test", connect=lambda _dsn: fake)
        self.assertTrue(fake.autocommit)
        row = adapter.execute("SELECT ?", (7,)).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row[0], 7)
        with adapter.transaction(write=True):
            adapter.execute("SELECT ?", (8,)).fetchall()
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with adapter.transaction(write=True):
                raise RuntimeError("abort")
        self.assertEqual(fake.calls, [("SELECT %s", (7,)), ("BEGIN", ()), ("SELECT %s", (8,)), ("BEGIN", ())])
        self.assertEqual(fake.commits, 1)
        self.assertEqual(fake.rollbacks, 1)
        self.assertTrue(all(cursor.closed for cursor in fake.cursors))
        adapter.close()
        self.assertTrue(fake.closed)

    def test_postgres_adapter_closes_failed_statement_cursor_and_rolls_back_claim(self):
        fake = _FakeConnection()
        adapter = PostgresAdapter.connect('postgresql://unit-test', connect=lambda _dsn: fake)
        with self.assertRaisesRegex(RuntimeError, 'query failed'):
            with adapter.transaction(write=True):
                fake.fail_execute = True
                adapter.execute('INSERT INTO orders VALUES (?)', ('id',))
        self.assertEqual(fake.commits, 0)
        self.assertEqual(fake.rollbacks, 1)
        self.assertTrue(all(cursor.closed for cursor in fake.cursors))
        adapter.close()

    def test_backend_url_never_silently_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            local = Path(root) / "local.sqlite3"
            adapter = backend_from_url(None, sqlite_path=local)
            self.assertIsInstance(adapter, SQLiteAdapter)
            adapter.close()
            with self.assertRaises(StorageBackendError):
                backend_from_url("mysql://unsupported", sqlite_path=local)
            with self.assertRaises(StorageBackendError):
                backend_from_url("", sqlite_path=None)

    def test_sqlite_url_preserves_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "orders.sqlite3"
            adapter = backend_from_url("sqlite:///" + str(path).lstrip("/"))
            adapter.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")
            adapter.close()
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
