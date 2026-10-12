"""Small SQL storage compatibility layer.

The decision ledger and order claim store currently use SQLite directly.  This
module isolates the parts that differ before a PostgreSQL migration: parameter
markers, transaction start, conflict-ignore inserts and JSON text extraction.
It deliberately does not select PostgreSQL implicitly and does not contain any
network or betting code.

PostgreSQL support is optional.  A deployment must provide ``psycopg`` (v3) or
``psycopg2`` and an explicit DSN; otherwise construction fails before any
schema or order write is attempted.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import importlib
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from store.postgres import PGResult


class StorageBackendError(RuntimeError):
    """The requested storage backend cannot be opened or used safely."""


class StorageDriverUnavailable(StorageBackendError):
    """The optional PostgreSQL DB-API driver is not installed."""


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _replace_markers(sql: str, marker: str) -> str:
    """Replace DB-API ``?`` markers outside quoted SQL text and comments.

    SQLite accepts ``?`` while psycopg uses ``%s``.  A regex replacement would
    corrupt a JSON/string literal containing a question mark, so this small
    scanner keeps quoted strings and SQL comments intact.
    """
    if marker == "?":
        return sql
    output: list[str] = []
    i = 0
    quote = ""
    while i < len(sql):
        char = sql[i]
        if quote:
            output.append(char)
            if char == quote:
                # SQL escapes a quote by doubling it.
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    output.append(sql[i + 1])
                    i += 2
                    continue
                quote = ""
            i += 1
            continue
        if char in ("'", '"', "`"):
            quote = char
            output.append(char)
            i += 1
            continue
        if char == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            end = sql.find("\n", i + 2)
            end = len(sql) if end < 0 else end
            output.append(sql[i:end])
            i = end
            continue
        if char == "/" and i + 1 < len(sql) and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            end = len(sql) if end < 0 else end + 2
            output.append(sql[i:end])
            i = end
            continue
        if char == "?":
            output.append(marker)
        else:
            output.append(char)
        i += 1
    return "".join(output)


@dataclass(frozen=True)
class SQLDialect:
    """SQL expressions that differ between the supported engines."""

    name: str
    marker: str

    def bind(self, sql: str) -> str:
        return _replace_markers(sql, self.marker)

    def placeholders(self, count: int) -> str:
        if count < 0:
            raise ValueError("占位符数量不能为负")
        return ",".join(self.marker for _ in range(count))

    def begin(self, *, write: bool = False) -> str:
        # SQLite's RESERVED write lock protects the order claim check/insert.
        # PostgreSQL obtains row/table locks through the statements themselves;
        # BEGIN cannot be upgraded to SQLite's BEGIN IMMEDIATE equivalent.
        return "BEGIN IMMEDIATE" if write and self.name == "sqlite" else "BEGIN"

    def json_text(self, column: str, key: str) -> str:
        if not column or not key or any(ch in column + key for ch in ";'\""):
            raise ValueError("JSON 字段名无效")
        if self.name == "postgres":
            return "(" + column + "::jsonb ->> '" + key + "')"
        return "json_extract(" + column + ", '$." + key + "')"

    def insert_ignore(self, table: str, columns: Sequence[str]) -> str:
        if (not table or not columns
                or any(not _IDENTIFIER.fullmatch(c) for c in (table, *columns))):
            raise ValueError("插入表或字段名无效")
        names = ",".join(columns)
        values = self.placeholders(len(columns))
        if self.name == "postgres":
            return "INSERT INTO " + table + " (" + names + ") VALUES (" + values + ") ON CONFLICT DO NOTHING"
        return "INSERT OR IGNORE INTO " + table + " (" + names + ") VALUES (" + values + ")"


SQLITE = SQLDialect("sqlite", "?")
POSTGRES = SQLDialect("postgres", "%s")


class SQLAdapter:
    """DB-API connection wrapper used by storage code and migration tests."""

    def __init__(self, connection: Any, dialect: SQLDialect) -> None:
        self.connection = connection
        self.dialect = dialect

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self.connection.execute(self.dialect.bind(sql), tuple(params))

    def executemany(self, sql: str, params: Sequence[Sequence[Any]]) -> Any:
        return self.connection.executemany(self.dialect.bind(sql), params)

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator["SQLAdapter"]:
        """Commit atomically or roll back on every exception.

        The wrapper does not hold a transaction during network calls.  Order
        stores must perform the duplicate check and durable claim within one
        short write transaction, then close it before calling a venue.
        """
        self.execute(self.dialect.begin(write=write))
        try:
            yield self
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class SQLiteAdapter(SQLAdapter):
    """SQLite implementation with the durability settings used today."""

    @classmethod
    def open(cls, path: Path, *, timeout: float = 10.0, read_only: bool = False) -> "SQLiteAdapter":
        if read_only:
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)
        else:
            connection = sqlite3.connect(path, timeout=timeout, isolation_level=None,
                                         check_same_thread=False)
            try:
                connection.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000)).close()
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        connection.execute("PRAGMA journal_mode=WAL").close()
                        break
                    except sqlite3.OperationalError as exc:
                        # Concurrent first connections can race while SQLite
                        # changes the journal mode; busy_timeout alone does not
                        # retry that initialization lock upgrade.
                        if (getattr(exc, 'sqlite_errorcode', None) not in
                                (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                                or time.monotonic() >= deadline):
                            raise
                        time.sleep(min(0.01, max(0.0, deadline-time.monotonic())))
                connection.execute("PRAGMA synchronous=FULL").close()
            except BaseException:
                connection.close()
                raise
        connection.row_factory = sqlite3.Row
        return cls(connection, SQLITE)


class PostgresAdapter(SQLAdapter):
    """Explicit PostgreSQL implementation using an optional DB-API driver."""

    @classmethod
    def connect(cls, dsn: str, *, connect: Callable[..., Any] | None = None) -> "PostgresAdapter":
        if not dsn.strip():
            raise StorageBackendError("PostgreSQL DSN 不能为空")
        if connect is None:
            connect = _postgres_connect()
        try:
            connection = connect(dsn)
            # Reads outside claim/update transactions must not retain a PG
            # snapshot indefinitely. transaction() still uses explicit BEGIN.
            connection.autocommit = True
        except Exception as exc:  # driver-specific connection errors
            raise StorageBackendError("PostgreSQL 连接失败") from exc
        return cls(connection, POSTGRES)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> PGResult:
        cursor = self.connection.cursor()
        try:
            cursor.execute(self.dialect.bind(sql), tuple(params))
            result = PGResult(cursor)
            if cursor.description is None:
                result.close()
            return result
        except BaseException:
            cursor.close()
            raise

    def executemany(self, sql: str, params: Sequence[Sequence[Any]]) -> PGResult:
        cursor = self.connection.cursor()
        try:
            cursor.executemany(self.dialect.bind(sql), params)
            result = PGResult(cursor)
            if cursor.description is None:
                result.close()
            return result
        except BaseException:
            cursor.close()
            raise


def _postgres_connect() -> Callable[..., Any]:
    for module_name in ("psycopg", "psycopg2"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        connect = getattr(module, "connect", None)
        if callable(connect):
            return connect
    raise StorageDriverUnavailable(
        "未安装 PostgreSQL 驱动；请在存储镜像中安装 psycopg[binary] 或 psycopg2-binary")


def backend_from_url(url: str | None, *, sqlite_path: Path | None = None) -> SQLAdapter:
    """Build an adapter only from an explicit URL.

    Empty configuration always returns SQLite when a path is supplied.  A
    PostgreSQL URL never silently falls back to local SQLite because that could
    make a successful betting/ledger write invisible to the intended database.
    """
    value = (url or "").strip()
    if not value:
        if sqlite_path is None:
            raise StorageBackendError("未配置 SQLite 路径")
        return SQLiteAdapter.open(sqlite_path)
    if value.startswith(("postgres://", "postgresql://")):
        return PostgresAdapter.connect(value)
    if value.startswith("sqlite:///"):
        # Strip only the ``sqlite://`` authority prefix; the remaining slash
        # is the leading slash of an absolute filesystem path.
        return SQLiteAdapter.open(Path(value[9:]))
    raise StorageBackendError("仅支持 sqlite:/// 或 postgresql:// 存储地址")


__all__ = [
    "POSTGRES", "SQLITE", "SQLAdapter", "SQLDialect", "SQLiteAdapter",
    "PostgresAdapter", "StorageBackendError", "StorageDriverUnavailable",
    "backend_from_url",
]
