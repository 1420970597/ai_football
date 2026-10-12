"""Small PostgreSQL compatibility layer used by the runtime stores.

The application historically used SQLite's qmark parameters and rows that can
be addressed by either column name or integer position.  Keeping that shape at
the storage boundary lets the business code move to PostgreSQL without
reintroducing a second local database or making tests depend on a live server.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any, Iterable, Iterator, Sequence


def postgres_enabled() -> bool:
    """Return whether runtime persistence must use the configured PG service."""
    backend = os.environ.get("DATABASE_BACKEND", "").strip().lower()
    if backend in {"sqlite", "local"}:
        return False
    return backend == "postgres" or all(os.environ.get(key) for key in (
        "PG_BASE_URL", "PG_BASE_PORT", "PG_DATASET", "PG_USER", "PG_PASSWORD"))


def _psycopg() -> Any:
    try:
        import psycopg
        from psycopg.rows import tuple_row
    except ImportError as exc:  # pragma: no cover - exercised in the image
        raise RuntimeError("PostgreSQL 后端需要 psycopg[binary] 依赖") from exc
    return psycopg, tuple_row


class CompatRow:
    """A row supporting ``row[0]``, ``row['column']`` and ``dict(row)``."""

    def __init__(self, values: Sequence[Any], names: Sequence[str]) -> None:
        self._values = tuple(values)
        self._names = tuple(names)
        self._mapping = dict(zip(self._names, self._values))

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._mapping[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> Iterable[str]:
        return self._mapping.keys()

    def items(self) -> Iterable[tuple[str, Any]]:
        return self._mapping.items()

    def values(self) -> Iterable[Any]:
        return self._mapping.values()

    def __contains__(self, key: object) -> bool:
        return key in self._mapping


class PGResult:
    def __init__(self, cursor: Any, on_close: Any = None) -> None:
        self._cursor = cursor
        self._on_close = on_close
        self._closed = False
        self.rowcount = int(getattr(cursor, "rowcount", -1))
        self._names = tuple(
            getattr(col, "name", col[0] if isinstance(col, (tuple, list)) else str(col))
            for col in (cursor.description or ())
        )

    def _row(self, row: Any) -> CompatRow | None:
        return CompatRow(row, self._names) if row is not None else None

    def fetchone(self) -> CompatRow | None:
        try:
            return self._row(self._cursor.fetchone())
        finally:
            self.close()

    def fetchall(self) -> list[CompatRow]:
        try:
            return [CompatRow(row, self._names) for row in self._cursor.fetchall()]
        finally:
            self.close()

    def __iter__(self) -> Iterator[CompatRow]:
        try:
            for row in self._cursor:
                yield CompatRow(row, self._names)
        finally:
            self.close()

    def close(self) -> None:
        """Release the server cursor after its result has been consumed."""
        if not self._closed:
            self._closed = True
            try:
                self._cursor.close()
            finally:
                if self._on_close is not None:
                    self._on_close()
                    self._on_close = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _qmark(sql: str) -> str:
    # Runtime SQL does not contain literal question marks. Keeping this
    # conversion in one place makes all old parameterized calls safe on PG.
    # psycopg parses percent signs even inside SQL string literals whenever
    # parameters are supplied. Preserve labels such as '80–100%' and LIKE
    # patterns before inserting our own parameter markers.
    return sql.replace("%", "%%").replace("?", "%s")


def _named(sql: str) -> str:
    """Translate SQLite's ``:name`` placeholders for psycopg mappings.

    PostgreSQL accepts ``%(name)s`` for mapping parameters.  Type casts such
    as ``::jsonb`` are excluded so the conversion cannot corrupt valid SQL.
    """
    return re.sub(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)", r"%(\1)s", sql.replace("%", "%%"))


def _translate(sql: str, params: Any = ()) -> str:
    sql = _named(sql) if isinstance(params, Mapping) else _qmark(sql)
    upper = sql.upper()
    if upper.startswith("INSERT OR IGNORE INTO "):
        sql = "INSERT INTO " + sql[len("INSERT OR IGNORE INTO "):]
        if ";" in sql:
            sql = sql.replace(";", " ON CONFLICT DO NOTHING;", 1)
        else:
            sql += " ON CONFLICT DO NOTHING"
    # The only runtime INSERT OR REPLACE is the imports checkpoint.  Orders
    # use an explicit identity claim and are handled by their own store.
    if upper.startswith("INSERT OR REPLACE INTO IMPORTS "):
        sql = "INSERT INTO " + sql[len("INSERT OR REPLACE INTO "):]
        sql += " ON CONFLICT (path) DO UPDATE SET size=EXCLUDED.size, mtime=EXCLUDED.mtime"
    return sql


def _postgres_params(params: Any) -> Any:
    """Map Python booleans to PostgreSQL integer flags used by legacy schema."""
    if isinstance(params, Mapping):
        return {key: (int(value) if isinstance(value, bool) else value)
                for key, value in params.items()}
    return tuple(int(value) if isinstance(value, bool) else value for value in params)


class PGConnection:
    """Connection facade with SQLite-compatible execute semantics."""

    backend = "postgres"

    def __init__(self) -> None:
        psycopg, tuple_row = _psycopg()
        self._connection = psycopg.connect(
            host=os.environ["PG_BASE_URL"], port=int(os.environ["PG_BASE_PORT"]),
            dbname=os.environ["PG_DATASET"], user=os.environ["PG_USER"],
            password=os.environ["PG_PASSWORD"], connect_timeout=10,
            row_factory=tuple_row, application_name="ai_football",
        )
        self._connection.autocommit = False
        self._explicit_transaction = False

    def execute(self, sql: str, params: Sequence[Any] = ()) -> PGResult:
        cursor = self._connection.cursor()
        try:
            normalized = _postgres_params(params)
            converted = _translate(sql, normalized)
            cursor.execute(converted, normalized)
        except BaseException:
            cursor.close()
            if not self._explicit_transaction:
                self._connection.rollback()
            raise
        command = converted.lstrip().split(None, 1)[0].upper() if converted.strip() else ''
        if command == 'BEGIN':
            self._explicit_transaction = True
        elif command in ('COMMIT', 'ROLLBACK'):
            self._explicit_transaction = False
        return PGResult(cursor, self._finish_result if not self._explicit_transaction else None)

    def _finish_result(self) -> None:
        """End implicit single-statement transactions after a result is read."""
        if not self._explicit_transaction:
            self._connection.commit()

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._connection.cursor() as cursor:
            rows = list(rows)
            mapping = bool(rows and isinstance(rows[0], Mapping))
            normalized = (_postgres_params(row) for row in rows)
            first = rows[0] if rows else ()
            cursor.executemany(_translate(sql, _postgres_params(first) if mapping else ()), normalized)

    def commit(self) -> None:
        self._connection.commit()
        self._explicit_transaction = False

    def rollback(self) -> None:
        self._connection.rollback()
        self._explicit_transaction = False

    def close(self) -> None:
        # A read-only API request may finish after a SELECT without reaching a
        # commit boundary. Roll it back before closing so the server does not
        # retain an idle transaction/snapshot while the connection object is
        # waiting for garbage collection.
        try:
            if not self._connection.closed:
                self._connection.rollback()
        finally:
            self._connection.close()

    def __enter__(self) -> "PGConnection":
        # Result consumption must not commit individual statements while a
        # caller relies on this context to commit or roll back them together.
        self._explicit_transaction = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


def connect() -> PGConnection:
    if not postgres_enabled():
        raise RuntimeError("PostgreSQL 后端未配置")
    return PGConnection()


def json_extract_function_sql() -> str:
    """Install SQLite-compatible JSON path extraction for legacy queries."""
    return """
    CREATE OR REPLACE FUNCTION json_extract(source text, path text)
    RETURNS text LANGUAGE SQL IMMUTABLE AS $$
      SELECT CASE WHEN source IS NULL THEN NULL
             ELSE (source::jsonb #>> string_to_array(regexp_replace(path, '^\\$\\.', ''), '.'))
             END
    $$
    """
