"""Durable order claims shared by SQLite and PostgreSQL deployments.

The order store owns only short persistence operations.  In particular, a
claim transaction is committed before the venue network request starts; an
ambiguous provider response remains an ``unknown`` receipt and is never
silently replayed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from store.sql_backend import (POSTGRES, SQLiteAdapter, SQLAdapter,
                               StorageBackendError, backend_from_url)
from store.postgres import connect as pg_connect, postgres_enabled


class OrderStore:
    """Backend-neutral storage for idempotent betting order receipts."""

    def __init__(self, adapter: SQLAdapter, *, path: Path | None = None) -> None:
        self.adapter = adapter
        self.path = path
        self.dialect = adapter.dialect
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def from_config(cls, root: str | None, url: str | None = None) -> "OrderStore":
        configured = (url if url is not None else
                      os.environ.get("BETTING_DB_URL") or
                      os.environ.get("ORDER_DB_URL") or "").strip()
        if not configured and postgres_configured():
            configured = postgres_dsn_from_env()
        if configured:
            if configured.startswith(("postgres://", "postgresql://")):
                # backend_from_url deliberately does not fall back to a local
                # file when a PostgreSQL URL is configured.
                return cls(backend_from_url(configured), path=None)
            if configured.startswith("sqlite:///"):
                path = Path(configured[9:]).expanduser()
                return cls(backend_from_url(configured, sqlite_path=path), path=path)
            raise StorageBackendError("仅支持 sqlite:/// 或 postgresql:// 订单存储地址")
        if not root:
            raise StorageBackendError("未配置持久化订单目录")
        if postgres_enabled():
            # DATABASE_BACKEND=postgres is the compose-wide switch.  Build the
            # connection from the split PG_* variables so credentials stay in
            # env_file and no local betting-orders.sqlite3 is created.
            return cls(SQLAdapter(pg_connect(), POSTGRES), path=None)
        path = Path(root).parent / "betting-orders.sqlite3"
        return cls(backend_from_url(None, sqlite_path=path), path=path)

    @property
    def is_postgres(self) -> bool:
        return self.dialect.name == POSTGRES.name

    def initialize(self) -> None:
        """Create the schema and indexes, migrating only old SQLite files."""
        with self._lock:
            if self._closed:
                raise StorageBackendError("订单存储已关闭")
            if isinstance(self.adapter, SQLiteAdapter):
                self._initialize_sqlite()
            else:
                self._initialize_postgres()

    def _initialize_sqlite(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.adapter.transaction(write=True):
                self.adapter.execute("""CREATE TABLE IF NOT EXISTS orders (
                    identity TEXT PRIMARY KEY, at REAL NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, selection_key TEXT NOT NULL, account_key TEXT)""")
                columns = {row[1] for row in self.adapter.execute("PRAGMA table_info(orders)")}
                for name in ("selection_key", "account_key"):
                    if name not in columns:
                        self.adapter.execute("ALTER TABLE orders ADD COLUMN " + name + " TEXT")
                for old_identity, raw in self.adapter.execute(
                        "SELECT identity,payload FROM orders WHERE selection_key IS NULL").fetchall():
                    previous = json.loads(raw)
                    if not isinstance(previous, dict) or not all(
                            previous.get(key) for key in ("match_id", "market", "outcome")):
                        raise StorageBackendError("历史订单标识不完整，无法安全去重")
                    # Import locally to avoid coupling the store to the
                    # executor's module at import time.
                    from service.betting import _bet_selection, _identity
                    selection = _identity(_bet_selection(str(previous["match_id"]), previous))
                    self.adapter.execute("UPDATE orders SET selection_key=? WHERE identity=?",
                                         (selection, old_identity))
                self.adapter.execute(
                    "CREATE INDEX IF NOT EXISTS orders_selection ON orders(selection_key,account_key)")
            os.chmod(self.path, 0o600)
        except sqlite3.Error as exc:
            raise StorageBackendError("订单数据库初始化失败") from exc

    def _initialize_postgres(self) -> None:
        # This branch intentionally contains no SQLite PRAGMA or qmark/ALTER
        # migration statements.  PostgreSQL deployments start with this full
        # schema and can apply migrations independently before rollout.
        with self.adapter.transaction(write=True):
            self.adapter.execute("""CREATE TABLE IF NOT EXISTS orders (
                identity TEXT PRIMARY KEY,
                at DOUBLE PRECISION NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                selection_key TEXT NOT NULL,
                account_key TEXT
            )""")
            self.adapter.execute(
                "CREATE INDEX IF NOT EXISTS orders_selection ON orders(selection_key,account_key)")
            self.adapter.execute(
                "CREATE INDEX IF NOT EXISTS orders_at ON orders(at DESC)")

    @staticmethod
    def _receipt(raw: Any) -> dict[str, Any]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(str(raw))
        if not isinstance(value, dict):
            raise ValueError("订单回执格式无效")
        if value.get("status") == "sending":
            value.update(status="unknown", submitted=None,
                         reason="上次提交未取得回执；请核对场馆注单")
        return value

    def existing_order(self, selection_key: str, account_key: str,
                       *, for_update: bool = False) -> dict[str, Any] | None:
        with self._lock:
            sql = ("SELECT payload FROM orders WHERE selection_key=? "
                   "AND (account_key=? OR account_key IS NULL) "
                   "ORDER BY at DESC,identity LIMIT 1")
            if for_update and self.is_postgres:
                sql += " FOR UPDATE"
            row = self.adapter.execute(sql, (selection_key, account_key)).fetchone()
            return None if row is None else {**self._receipt(row[0]), "duplicate": True}

    def claim(self, identity: str, sending: Mapping[str, Any], selection_key: str,
              account_key: str, *, authorize: Callable[[Callable[[], Any]], Any] | None = None,
              on_duplicate: Callable[[dict[str, Any]], dict[str, Any]] | None = None
              ) -> tuple[bool, dict[str, Any] | None]:
        """Atomically check and reserve an order before any provider call."""
        with self._lock:
            with self.adapter.transaction(write=True):
                previous = self.existing_order(selection_key, account_key,
                                                for_update=self.is_postgres)
                if previous is not None:
                    return False, previous if on_duplicate is None else on_duplicate(previous)

                insert_sql = self.dialect.insert_ignore(
                    "orders", ("identity", "at", "status", "payload", "selection_key", "account_key"))
                params = (identity, __import__("time").time(), "sending",
                          json.dumps(dict(sending), ensure_ascii=False), selection_key, account_key)
                if authorize is None:
                    cursor = self.adapter.execute(insert_sql, params)
                else:
                    cursor = authorize(lambda: self.adapter.execute(insert_sql, params))
                if getattr(cursor, "rowcount", 1) == 0:
                    previous = self.existing_order(selection_key, account_key,
                                                    for_update=self.is_postgres)
                    if previous is not None:
                        return False, previous
                    return False, {"status": "duplicate", "duplicate": True,
                                   "reason": "该推荐已有订单提交记录"}
                return True, None

    def update(self, identity: str, status: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            with self.adapter.transaction(write=True):
                self.adapter.execute("UPDATE orders SET status=?,payload=? WHERE identity=?",
                                     (status, json.dumps(dict(payload), ensure_ascii=False), identity))

    def orders_for_selections(self, selection_keys: list[str], account_key: str
                              ) -> list[dict[str, Any]]:
        if not selection_keys:
            return []
        with self._lock:
            result: list[dict[str, Any]] = []
            for offset in range(0, len(selection_keys), 200):
                batch = selection_keys[offset:offset + 200]
                rows = self.adapter.execute(
                    "SELECT payload FROM orders WHERE selection_key IN (" +
                    ",".join(self.dialect.marker for _ in batch) +
                    ") AND (account_key=? OR account_key IS NULL) ORDER BY at",
                    (*batch, account_key)).fetchall()
                result.extend(self._receipt(row[0]) for row in rows)
            return result

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 0:
            raise ValueError("订单数量不能为负")
        with self._lock:
            rows = self.adapter.execute("SELECT payload FROM orders ORDER BY at DESC LIMIT ?",
                                        (int(limit),)).fetchall()
            return [self._receipt(row[0]) for row in rows]

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self.adapter.close()
                self._closed = True

    def __del__(self) -> None:  # pragma: no cover - interpreter cleanup
        try:
            self.close()
        except Exception:
            pass


__all__ = ["OrderStore"]


def postgres_configured() -> bool:
    """Whether the deployment explicitly selected the PostgreSQL backend."""
    backend = os.environ.get("DATABASE_BACKEND", "").strip().lower()
    if backend in {"sqlite", "local"}:
        return False
    return backend == "postgres" or all(os.environ.get(key, "").strip() for key in (
        "PG_BASE_URL", "PG_BASE_PORT", "PG_DATASET", "PG_USER", "PG_PASSWORD"))


def postgres_dsn_from_env() -> str:
    """Build a libpq DSN without logging or exposing the password."""
    required = ("PG_BASE_URL", "PG_BASE_PORT", "PG_DATASET", "PG_USER", "PG_PASSWORD")
    missing = [key for key in required if not os.environ.get(key, "").strip()]
    if missing:
        raise StorageBackendError("PostgreSQL 配置缺少：" + ",".join(missing))
    return ("postgresql://" + quote(os.environ["PG_USER"], safe="") + ":" +
            quote(os.environ["PG_PASSWORD"], safe="") + "@" +
            os.environ["PG_BASE_URL"] + ":" + os.environ["PG_BASE_PORT"] + "/" +
            quote(os.environ["PG_DATASET"], safe=""))


__all__ += ["postgres_configured", "postgres_dsn_from_env"]
