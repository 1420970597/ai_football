"""Read-only database boundary for model-training projections.

The live system currently stores its indexed projections in SQLite files.  This
module keeps that behaviour while defining the boundary needed for a future
PostgreSQL projection.  It deliberately has no PostgreSQL package dependency:
the worker can keep running with SQLite, and a PostgreSQL deployment fails with
an actionable error when its optional DB-API driver is missing.

The connection is read-only for the persistent database but permits temporary
tables.  ``store.training_data`` uses temporary tables to materialise labels,
so using SQLite's ``mode=ro`` (rather than ``PRAGMA query_only``) is important.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import importlib
import math
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator, Literal, Protocol, Sequence
from urllib.parse import quote, urlsplit


Backend = Literal["sqlite", "postgresql"]


class ProjectionConfigError(ValueError):
    """Raised when a projection DSN is invalid or unsupported."""


class ProjectionDependencyError(RuntimeError):
    """Raised when a configured backend has no installed DB-API driver."""


class ProjectionStorageError(RuntimeError):
    """Raised when a configured projection cannot be opened or written."""


class CursorLike(Protocol):
    def execute(self, operation: str, parameters: Sequence[Any] = ()) -> Any: ...

    def fetchmany(self, size: int = 1) -> list[Any]: ...


@dataclass(frozen=True)
class ProjectionTarget:
    """A validated training projection target.

    ``location`` is a local path for SQLite and the original DSN for
    PostgreSQL.  The DSN is never included in the default representation so a
    password cannot accidentally enter status files or logs.
    """

    backend: Backend
    location: str
    connect_timeout_s: float = 5.0
    statement_timeout_s: float = 600.0
    fetch_size: int = 2000

    def __post_init__(self) -> None:
        if self.connect_timeout_s <= 0 or not math.isfinite(self.connect_timeout_s):
            raise ProjectionConfigError("connect_timeout_s 必须为正数")
        if self.statement_timeout_s <= 0 or not math.isfinite(self.statement_timeout_s):
            raise ProjectionConfigError("statement_timeout_s 必须为正数")
        if isinstance(self.fetch_size, bool) or not isinstance(self.fetch_size, int) or self.fetch_size < 1:
            raise ProjectionConfigError("fetch_size 必须为正整数")
        if self.backend not in ("sqlite", "postgresql") or not self.location:
            raise ProjectionConfigError("训练投影后端或地址无效")

    @classmethod
    def parse(
        cls,
        value: str | Path,
        *,
        connect_timeout_s: float = 5.0,
        statement_timeout_s: float = 600.0,
        fetch_size: int = 2000,
    ) -> "ProjectionTarget":
        """Parse a local SQLite path, ``sqlite:///`` URI, or PostgreSQL DSN."""
        raw = str(value).strip()
        if not raw:
            raise ProjectionConfigError("训练投影地址不能为空")
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        if not scheme:
            return cls("sqlite", str(Path(raw).expanduser()), connect_timeout_s,
                       statement_timeout_s, fetch_size)
        if scheme in ("sqlite", "sqlite3"):
            if parsed.netloc and parsed.netloc not in ("", "localhost"):
                raise ProjectionConfigError("SQLite URI 不支持远程主机")
            path = parsed.path
            if not path:
                raise ProjectionConfigError("SQLite URI 缺少数据库路径")
            return cls("sqlite", path, connect_timeout_s, statement_timeout_s, fetch_size)
        if scheme in ("postgres", "postgresql"):
            # A PostgreSQL DSN may contain a Unix socket path and therefore no
            # hostname.  Keep validation deliberately light and let the driver
            # report authentication/network errors without exposing secrets.
            return cls("postgresql", raw, connect_timeout_s, statement_timeout_s, fetch_size)
        raise ProjectionConfigError("不支持的训练投影协议: " + scheme)

    def __repr__(self) -> str:
        return (f"ProjectionTarget(backend={self.backend!r}, "
                f"location={self.redacted_location()!r}, "
                f"connect_timeout_s={self.connect_timeout_s!r}, "
                f"statement_timeout_s={self.statement_timeout_s!r}, "
                f"fetch_size={self.fetch_size!r})")

    def redacted_location(self) -> str:
        if self.backend == "sqlite":
            return self.location
        parsed = urlsplit(self.location)
        if parsed.password is None and parsed.username is None:
            return self.location
        host = parsed.hostname or ""
        if parsed.port:
            host += ":" + str(parsed.port)
        user = (parsed.username or "") + (":***" if parsed.password is not None else "")
        auth = user + ("@" if user else "")
        return f"{parsed.scheme}://{auth}{host}{parsed.path}"

    def sqlite_uri(self) -> str:
        if self.backend != "sqlite":
            raise ProjectionConfigError("当前投影不是 SQLite")
        path = Path(self.location).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        return path.as_uri() + "?mode=ro"


def _load_postgres_driver() -> Any:
    for name in ("psycopg", "psycopg2"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise ProjectionDependencyError(
        "PostgreSQL 训练投影需要可选依赖 psycopg（或 psycopg2）；当前环境未安装")


def _configure_postgres(connection: Any, timeout_s: float) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        milliseconds = str(max(1, round(timeout_s * 1000)))
        cursor.execute("SELECT set_config('statement_timeout', %s, false)", (milliseconds,))
        connection.commit()
    finally:
        cursor.close()


def _connect_postgres(target: ProjectionTarget, driver: Any | None = None) -> Any:
    module = driver or _load_postgres_driver()
    timeout = max(1, round(target.connect_timeout_s))
    try:
        connection = module.connect(target.location, connect_timeout=timeout)
    except TypeError:
        # A small number of DB-API wrappers reject connect_timeout as a keyword;
        # the server-side statement timeout remains enforced after connecting.
        connection = module.connect(target.location)
    return connection


@contextmanager
def readonly_connection(target: ProjectionTarget, *, driver: Any | None = None) -> Iterator[Any]:
    """Open a persistent read-only connection, allowing temporary tables.

    SQLite's URI ``mode=ro`` protects the source file while still allowing the
    temporary label table used by the current training query.  PostgreSQL uses
    session-level read-only transactions; temporary tables remain available to
    the same snapshot.
    """
    if target.backend == "sqlite":
        connection = sqlite3.connect(target.sqlite_uri(), uri=True,
                                     timeout=target.connect_timeout_s,
                                     check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={max(1, round(target.connect_timeout_s * 1000))}")
        try:
            yield connection
        finally:
            connection.close()
        return

    connection = _connect_postgres(target, driver)
    try:
        _configure_postgres(connection, target.statement_timeout_s)
        yield connection
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


@contextmanager
def writable_connection(target: ProjectionTarget, *, driver: Any | None = None) -> Iterator[Any]:
    """Open a PostgreSQL writer connection for model projection tables.

    This is intentionally separate from :func:`readonly_connection`: only the
    model service uses it for idempotent input/prediction upserts.  Training
    queries must never receive this connection.
    """
    if target.backend != "postgresql":
        raise ProjectionConfigError("训练投影写入目前只支持 PostgreSQL")
    connection = _connect_postgres(target, driver)
    try:
        cursor = connection.cursor()
        try:
            milliseconds = str(max(1, round(target.statement_timeout_s * 1000)))
            cursor.execute("SELECT set_config('statement_timeout', %s, false)", (milliseconds,))
        finally:
            cursor.close()
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


_PERSISTENT_WRITER = threading.local()


def close_persistent_writer() -> None:
    connection = getattr(_PERSISTENT_WRITER,'connection',None)
    _PERSISTENT_WRITER.connection = None
    if connection is not None:
        connection.close()


@contextmanager
def persistent_writer(target: ProjectionTarget) -> Iterator[Any]:
    """Thread-owned live writer; reconnect on failure, never share sessions."""
    if target.backend != 'postgresql':
        raise ProjectionConfigError('实时持久化连接必须是PostgreSQL')
    connection = getattr(_PERSISTENT_WRITER,'connection',None)
    if getattr(_PERSISTENT_WRITER,'target',None) != target:
        close_persistent_writer()
        connection = None
    try:
        if connection is None:
            connection = _connect_postgres(target)
            _PERSISTENT_WRITER.connection = connection
            _PERSISTENT_WRITER.target = target
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT set_config('statement_timeout', %s, false)",
                               (str(max(1,round(target.statement_timeout_s*1000))),))
            finally:
                cursor.close()
        yield connection
        connection.commit()
    except Exception:
        try:
            if connection is not None:
                connection.rollback()
        finally:
            close_persistent_writer()
        raise


@contextmanager
def readonly_snapshot(target: ProjectionTarget, *, driver: Any | None = None) -> Iterator[Any]:
    """Open one repeatable read snapshot for a bounded training pass."""
    with readonly_connection(target, driver=driver) as connection:
        if target.backend == "sqlite":
            connection.execute("BEGIN")
        else:
            cursor = connection.cursor()
            try:
                cursor.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            finally:
                cursor.close()
        try:
            yield connection
        finally:
            connection.rollback()


def fetch_batches(cursor: CursorLike, operation: str, parameters: Sequence[Any] = (),
                  *, batch_size: int = 2000) -> Iterator[list[Any]]:
    """Stream query results without accumulating a full training projection."""
    if isinstance(batch_size, bool) or batch_size < 1:
        raise ProjectionConfigError("batch_size 必须为正整数")
    cursor.execute(operation, parameters)
    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            return
        yield batch


def projection_target_from_env() -> ProjectionTarget | None:
    """Return the explicitly configured remote projection, if any.

    An empty variable means the legacy local SQLite paths remain active.  No
    implicit localhost PostgreSQL fallback is allowed because that could train
    against an empty or stale database without a visible error.
    """
    raw = os.environ.get("TRAINING_PROJECTION_DSN", "").strip()
    if not raw and os.environ.get("DATABASE_BACKEND", "").strip().lower() == "postgres":
        required = ("PG_BASE_URL", "PG_BASE_PORT", "PG_DATASET", "PG_USER", "PG_PASSWORD")
        missing = [key for key in required if not os.environ.get(key)]
        if missing:
            raise ProjectionConfigError("PostgreSQL 已启用但缺少配置: " + ",".join(missing))
        raw = "postgresql://%s:%s@%s:%s/%s" % (
            quote(os.environ["PG_USER"], safe=""), quote(os.environ["PG_PASSWORD"], safe=""),
            os.environ["PG_BASE_URL"], os.environ["PG_BASE_PORT"],
            quote(os.environ["PG_DATASET"], safe=""))
    if not raw:
        return None
    return ProjectionTarget.parse(
        raw,
        connect_timeout_s=float(os.environ.get("TRAINING_PROJECTION_CONNECT_TIMEOUT_S", "5")),
        statement_timeout_s=float(os.environ.get("TRAINING_PROJECTION_STATEMENT_TIMEOUT_S", "600")),
        fetch_size=int(os.environ.get("TRAINING_PROJECTION_FETCH_SIZE", "2000")),
    )


POSTGRES_INPUTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_inputs (
    match_id TEXT NOT NULL,
    at TEXT NOT NULL,
    cutoff_ms DOUBLE PRECISION NOT NULL,
    algorithm TEXT NOT NULL,
    market TEXT NOT NULL,
    line TEXT NOT NULL,
    outcome TEXT NOT NULL,
    baseline DOUBLE PRECISION NOT NULL,
    x TEXT NOT NULL,
    decision_received_at_ms DOUBLE PRECISION,
    quote_received_at_ms DOUBLE PRECISION,
    entry_score TEXT,
    settlement_basis TEXT,
    model_version TEXT,
    data_version TEXT,
    feature_version TEXT,
    PRIMARY KEY (match_id, at, algorithm, market, line, outcome)
)
"""

POSTGRES_LABELS_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_labels (
    match_id TEXT NOT NULL,
    available_at TEXT NOT NULL,
    ft_score TEXT NOT NULL,
    ht_score TEXT,
    status TEXT NOT NULL,
    revision_id TEXT PRIMARY KEY,
    revision_order DOUBLE PRECISION NOT NULL DEFAULT 0
)
"""

POSTGRES_LABEL_REVISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS label_revisions (
    revision_id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL,
    available_at TEXT NOT NULL,
    ft_score TEXT NOT NULL,
    ht_score TEXT,
    status TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    revision_order DOUBLE PRECISION NOT NULL DEFAULT 0
)
"""

POSTGRES_PREDICTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_predictions (
    version TEXT NOT NULL,
    match_id TEXT NOT NULL,
    at TEXT NOT NULL,
    cutoff_ms DOUBLE PRECISION NOT NULL,
    algorithm TEXT NOT NULL,
    market TEXT NOT NULL,
    line TEXT NOT NULL,
    outcome TEXT NOT NULL,
    baseline DOUBLE PRECISION NOT NULL,
    x TEXT NOT NULL,
    probability DOUBLE PRECISION NOT NULL,
    predicted_at TEXT,
    decision_received_at_ms DOUBLE PRECISION,
    quote_received_at_ms DOUBLE PRECISION,
    entry_score TEXT,
    settlement_basis TEXT,
    model_version TEXT,
    data_version TEXT,
    feature_version TEXT,
    repair_status TEXT,
    original_x_sha256 TEXT,
    original_probability DOUBLE PRECISION,
    PRIMARY KEY (version, match_id, at, algorithm, market, line, outcome)
)
"""

SHADOW_LABEL_TIMESTAMP_PATTERN = r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$'
SHADOW_LABEL_SCORE_PATTERN = (r'^[[:space:]]*\[[[:space:]]*(100|[0-9]{1,2})[[:space:]]*,'
                              r'[[:space:]]*(100|[0-9]{1,2})[[:space:]]*\][[:space:]]*$')
SHADOW_LABEL_EPOCH_FUNCTION = """
CREATE OR REPLACE FUNCTION shadow_label_available_epoch(value TEXT)
RETURNS DOUBLE PRECISION LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
SET TimeZone TO 'UTC' SET DateStyle TO 'ISO, YMD' AS $$
SELECT CASE WHEN value ~ '%s' AND pg_input_is_valid(value,'timestamp with time zone')
THEN EXTRACT(EPOCH FROM value::timestamptz)::double precision ELSE NULL END
$$
""" % SHADOW_LABEL_TIMESTAMP_PATTERN

SHADOW_LABEL_INDEXES = tuple(
    f'CREATE INDEX IF NOT EXISTS idx_{table}_shadow_available ON {table} '
    '(shadow_label_available_epoch(available_at),revision_id,match_id) '
    "WHERE status IN ('won','lost','half_won','half_lost','push','void') "
    'AND shadow_label_available_epoch(available_at) IS NOT NULL '
    "AND ft_score ~ '" + SHADOW_LABEL_SCORE_PATTERN + "'"
    for table in ('model_labels','label_revisions'))

_PREPARED_SCHEMA_LOCATIONS: set[str] = set()
_SCHEMA_PREPARATION_LOCK = threading.Lock()


def prepare_postgres_schema(target: ProjectionTarget) -> None:
    """Commit DDL once, separately from normal input/prediction upserts.

    Holding the schema advisory lock until a large input batch commits makes
    an independent live writer wait for historical replay. Successful setup
    is cached per process/DSN; failures remain retryable.
    """
    with _SCHEMA_PREPARATION_LOCK:
        if target.location in _PREPARED_SCHEMA_LOCATIONS:
            return
        with writable_connection(target) as connection:
            ensure_postgres_schema(connection)
        _PREPARED_SCHEMA_LOCATIONS.add(target.location)


def ensure_postgres_schema(connection: Any) -> None:
    """Create the model projection tables and indexes on a writer connection."""
    cursor = connection.cursor()
    try:
        # API, replay, archive and model-worker processes can all start at
        # once after a deploy. Without serialization, each transaction may
        # observe a missing index and scan the 23M-row projection concurrently
        # under CREATE INDEX IF NOT EXISTS. A transaction-scoped advisory lock
        # makes schema initialization single-writer while keeping normal data
        # writes concurrent after initialization.
        cursor.execute("SELECT pg_advisory_xact_lock(1984, 4201)")
        for statement in (POSTGRES_INPUTS_SCHEMA, POSTGRES_LABELS_SCHEMA,
                          POSTGRES_LABEL_REVISIONS_SCHEMA, POSTGRES_PREDICTIONS_SCHEMA):
            cursor.execute(statement)
        # Only explicit ISO timestamps with a numeric offset/Z are admitted.
        # Their epoch is independent of session TimeZone; marking this safe
        # wrapper immutable permits a true indexed chronological label cursor.
        cursor.execute(SHADOW_LABEL_EPOCH_FUNCTION)
        for column, kind in (("repair_status", "TEXT"), ("original_x_sha256", "TEXT"),
                             ("original_probability", "DOUBLE PRECISION"),
                             ("decision_received_at_ms", "DOUBLE PRECISION"),
                             ("quote_received_at_ms", "DOUBLE PRECISION")):
            cursor.execute("ALTER TABLE model_predictions ADD COLUMN IF NOT EXISTS " + column + " " + kind)
        for statement in (
            # The migration's idx_migrate_model_inputs has this exact key
            # order. Reuse it so cutover never scans the 23M-row projection
            # a second time merely because the runtime chose another name.
            "CREATE INDEX IF NOT EXISTS idx_migrate_model_inputs ON model_inputs "
            "(data_version, model_version, feature_version, match_id, at)",
            "CREATE INDEX IF NOT EXISTS idx_model_predictions_version ON model_predictions "
            "(version, data_version, model_version, at)",
            "CREATE INDEX IF NOT EXISTS idx_model_predictions_observation ON model_predictions "
            "(version,data_version,model_version,feature_version,predicted_at,at,match_id,algorithm,market,line,outcome)",
            "CREATE INDEX IF NOT EXISTS idx_model_labels_available ON model_labels "
            "(available_at, match_id)",
            "CREATE INDEX IF NOT EXISTS idx_label_revisions_match_time ON label_revisions "
            "(match_id, available_at)",
        ):
            cursor.execute(statement)
        for statement in SHADOW_LABEL_INDEXES:
            cursor.execute(statement)
    finally:
        cursor.close()
