from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from store.order_store import OrderStore
from store.sql_backend import PostgresAdapter


class _Result:
    def __init__(self, rows=(), rowcount: int = 0) -> None:
        self.rows = list(rows)
        self.rowcount = rowcount
        self.description = None
        self.closed = False

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        self.closed = True


class _MemoryCursor(_Result):
    def __init__(self, connection):
        super().__init__()
        self.connection = connection

    def execute(self, sql, params=()):
        result = self.connection.execute(sql, params)
        self.rows = result.rows
        self.rowcount = result.rowcount
        self.description = (('payload',),) if sql.upper().startswith('SELECT') else None


class _PostgresMemoryConnection:
    """Tiny DB-API fake that models only the order-store statements."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.rows: dict[str, tuple[str, str, str, str, str | None]] = {}
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.cursors: list[_MemoryCursor] = []
        self.autocommit = False

    def cursor(self):
        cursor = _MemoryCursor(self)
        self.cursors.append(cursor)
        return cursor

    def execute(self, sql: str, params=()):
        params = tuple(params)
        self.calls.append((sql, params))
        upper = sql.upper()
        if upper.startswith("SELECT PAYLOAD FROM ORDERS WHERE SELECTION_KEY"):
            selection, account = params
            candidates = [row for row in self.rows.values()
                          if row[3] == selection and (row[4] == account or row[4] is None)]
            candidates.sort(key=lambda row: row[1], reverse=True)
            return _Result([(candidates[0][0],)] if candidates else [])
        if upper.startswith("INSERT INTO ORDERS"):
            identity, at, status, payload, selection, account = params
            if identity in self.rows:
                return _Result(rowcount=0)
            self.rows[identity] = (payload, at, status, selection, account)
            return _Result(rowcount=1)
        if upper.startswith("SELECT PAYLOAD FROM ORDERS ORDER BY AT"):
            return _Result([(row[0],) for row in self.rows.values()])
        return _Result(rowcount=1)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class OrderStorePostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = _PostgresMemoryConnection()
        adapter = PostgresAdapter.connect("postgresql://unit-test", connect=lambda _dsn: self.connection)
        self.store = OrderStore(adapter)
        self.store.initialize()
        self.addCleanup(self.store.close)

    def test_schema_is_postgres_specific_and_has_no_sqlite_migration(self) -> None:
        statements = [sql.upper() for sql, _ in self.connection.calls]
        self.assertTrue(any("DOUBLE PRECISION" in sql for sql in statements))
        self.assertTrue(any("CREATE INDEX" in sql for sql in statements))
        self.assertFalse(any("PRAGMA" in sql for sql in statements))
        self.assertFalse(any("ALTER TABLE" in sql for sql in statements))
        self.assertFalse(any("INSERT OR IGNORE" in sql for sql in statements))

    def test_order_reads_use_autocommit_and_release_all_result_cursors(self) -> None:
        self.assertTrue(self.connection.autocommit)
        self.assertIsNone(self.store.existing_order('missing', 'account'))
        self.assertEqual(self.store.recent(), [])
        self.assertTrue(all(cursor.closed for cursor in self.connection.cursors))

    def test_claim_is_idempotent_and_stores_sending_intent(self) -> None:
        sending = {"identity": "id-1", "match_id": "m", "status": "sending"}
        inserted, previous = self.store.claim("id-1", sending, "selection", "account")
        self.assertTrue(inserted)
        self.assertIsNone(previous)
        inserted, previous = self.store.claim("id-1", sending, "selection", "account")
        self.assertFalse(inserted)
        self.assertIsNotNone(previous)
        assert previous is not None
        self.assertTrue(previous["duplicate"])
        self.assertEqual(json.loads(next(iter(self.connection.rows.values()))[0])["status"], "sending")
        self.assertGreaterEqual(self.connection.commits, 2)

    def test_claim_failure_rolls_back(self) -> None:
        original = self.connection.execute

        def fail_insert(sql, params=()):
            if sql.upper().startswith("INSERT INTO ORDERS"):
                raise RuntimeError("insert failed")
            return original(sql, params)

        self.connection.execute = fail_insert
        with self.assertRaises(RuntimeError):
            self.store.claim("id-2", {"status": "sending"}, "selection-2", "account")
        self.assertEqual(self.connection.rollbacks, 1)

    def test_explicit_postgres_url_does_not_create_local_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with patch("store.sql_backend.PostgresAdapter.connect",
                       return_value=self.store.adapter):
                store = OrderStore.from_config(str(Path(root) / "ledger"),
                                               "postgresql://configured")
            self.assertIsNone(store.path)
            self.assertFalse((Path(root) / "betting-orders.sqlite3").exists())

    def test_global_postgres_backend_is_used_without_order_specific_url(self) -> None:
        values = {
            "DATABASE_BACKEND": "postgres",
            "PG_BASE_URL": "db.example",
            "PG_BASE_PORT": "5432",
            "PG_DATASET": "football",
            "PG_USER": "worker",
            "PG_PASSWORD": "secret",
        }
        with tempfile.TemporaryDirectory() as root, patch.dict("os.environ", values, clear=False), \
                patch("store.sql_backend.PostgresAdapter.connect",
                      return_value=self.store.adapter) as connect:
            store = OrderStore.from_config(str(Path(root) / "ledger"))
            self.assertIsNone(store.path)
            self.assertFalse((Path(root) / "betting-orders.sqlite3").exists())
            connect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
