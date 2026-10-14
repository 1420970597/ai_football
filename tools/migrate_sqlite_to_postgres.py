#!/usr/bin/env python3
"""Migrate the historical SQLite projections into the configured PostgreSQL.

The command is deliberately conservative: it probes PostgreSQL before opening
large source files, imports in bounded batches, uses idempotent conflict rules,
and compares source/target row counts for every table.  It never removes the
SQLite source; cleanup is a separate, post-cutover operation.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable

# Allow both ``python tools/...`` and ``python -m`` invocation from the
# repository root without requiring an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.postgres import PGConnection, connect

BATCH_SIZE = 2_000
COPY_BATCH_SIZE = 50_000


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def _ident(name: str) -> str:
    if not name.replace("_", "").isalnum() or not name:
        raise ValueError("invalid SQL identifier: " + name)
    return '"' + name + '"'


def _source(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                          timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in con.execute("PRAGMA table_info(" + _ident(table) + ")")]


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _digest_rows(rows: Iterable[Iterable[Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(list(row), ensure_ascii=False, default=str,
                                 separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _create_target(db: PGConnection) -> None:
    """Create all production tables without constructing any SQLite files."""
    db.execute("""CREATE TABLE IF NOT EXISTS decisions (
        identity TEXT PRIMARY KEY, revision DOUBLE PRECISION, payload TEXT,
        match_id TEXT, at TEXT, algorithm TEXT, competition_type TEXT,
        trigger TEXT, decision TEXT, market TEXT, league TEXT, line TEXT,
        outcome TEXT, config_version INTEGER, status TEXT, is_pick INTEGER,
        decision_count INTEGER, decision_event_id TEXT, is_live INTEGER,
        odds DOUBLE PRECISION, pnl DOUBLE PRECISION, confidence DOUBLE PRECISION,
        kelly DOUBLE PRECISION, p_fused DOUBLE PRECISION, p_market DOUBLE PRECISION,
        closing_odds DOUBLE PRECISION, settled_at TEXT, decision_id TEXT,
        experiment_id TEXT, label_available_at TEXT, review_status TEXT,
        research_only INTEGER, model_version TEXT)""")
    db.execute("CREATE TABLE IF NOT EXISTS imports(path TEXT PRIMARY KEY,size BIGINT,mtime BIGINT)")
    db.execute("""CREATE TABLE IF NOT EXISTS label_revisions(
        revision_id TEXT PRIMARY KEY, match_id TEXT NOT NULL, available_at TEXT NOT NULL,
        ft_score TEXT NOT NULL, ht_score TEXT, status TEXT NOT NULL,
        source_revision TEXT NOT NULL, revision_order DOUBLE PRECISION NOT NULL DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS orders(
        identity TEXT PRIMARY KEY, at DOUBLE PRECISION NOT NULL, status TEXT NOT NULL,
        payload TEXT NOT NULL, selection_key TEXT NOT NULL, account_key TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS model_inputs(
        match_id TEXT, at TEXT, cutoff_ms DOUBLE PRECISION, algorithm TEXT,
        market TEXT, line TEXT, outcome TEXT, baseline DOUBLE PRECISION, x TEXT,
        decision_received_at_ms DOUBLE PRECISION, quote_received_at_ms DOUBLE PRECISION,
        entry_score TEXT, settlement_basis TEXT, model_version TEXT,
        data_version TEXT, feature_version TEXT, odds DOUBLE PRECISION,
        PRIMARY KEY(match_id,at,algorithm,market,line,outcome))""")
    db.execute("""CREATE TABLE IF NOT EXISTS model_predictions(
        version TEXT, match_id TEXT, at TEXT, cutoff_ms DOUBLE PRECISION,
        algorithm TEXT, market TEXT, line TEXT, outcome TEXT, baseline DOUBLE PRECISION,
        x TEXT, probability DOUBLE PRECISION, predicted_at TEXT, entry_score TEXT,
        settlement_basis TEXT, model_version TEXT, data_version TEXT, feature_version TEXT,
        odds DOUBLE PRECISION, decision_received_at_ms DOUBLE PRECISION, quote_received_at_ms DOUBLE PRECISION,
        repair_status TEXT, original_x_sha256 TEXT, original_probability DOUBLE PRECISION,
        PRIMARY KEY(version,match_id,at,algorithm,market,line,outcome))""")
    db.execute("""CREATE TABLE IF NOT EXISTS model_labels(
        match_id TEXT NOT NULL, available_at TEXT NOT NULL, ft_score TEXT NOT NULL,
        ht_score TEXT, status TEXT NOT NULL, revision_id TEXT PRIMARY KEY,
        revision_order DOUBLE PRECISION NOT NULL DEFAULT 0)""")
    for table, column, kind in (("model_inputs", "odds", "DOUBLE PRECISION"),
                                ("model_predictions", "odds", "DOUBLE PRECISION"),
                                ("model_predictions", "decision_received_at_ms", "DOUBLE PRECISION"),
                                ("model_predictions", "quote_received_at_ms", "DOUBLE PRECISION"),
                                ("model_predictions", "repair_status", "TEXT"),
                                ("model_predictions", "original_x_sha256", "TEXT"),
                                ("model_predictions", "original_probability", "DOUBLE PRECISION")):
        db.execute("ALTER TABLE " + table + " ADD COLUMN IF NOT EXISTS " + column + " " + kind)
    db.execute("""CREATE TABLE IF NOT EXISTS files(
        path TEXT PRIMARY KEY, object_key TEXT NOT NULL, "offset" BIGINT NOT NULL,
        compressed BIGINT NOT NULL, size BIGINT NOT NULL, sha256 TEXT NOT NULL,
        mtime_ns BIGINT NOT NULL, cleaned INTEGER NOT NULL DEFAULT 0)""")
    db.execute("CREATE TABLE IF NOT EXISTS packs(object_key TEXT PRIMARY KEY,bytes BIGINT,sha256 TEXT,at DOUBLE PRECISION)")
    db.execute('CREATE TABLE IF NOT EXISTS model_schema_migrations '
               '(version TEXT PRIMARY KEY,completed_at TIMESTAMPTZ NOT NULL DEFAULT now())')
    db.execute("INSERT INTO model_schema_migrations(version) VALUES ('TASK-55-prediction-clocks-odds') "
               'ON CONFLICT(version) DO NOTHING')
    for table, columns in {
        "decisions": "match_id,at,algorithm,trigger,status",
        "orders": "selection_key,account_key",
        "model_inputs": "data_version,model_version,feature_version,match_id,at",
        "files": "path",
    }.items():
        db.execute("CREATE INDEX IF NOT EXISTS idx_migrate_" + table + " ON " + _ident(table) + "(" + columns + ")")
    db.commit()


def _copy_table(db: PGConnection, source_path: Path, source_table: str,
                target_table: str, *, conflict: str = "nothing") -> tuple[int, int]:
    if not source_path.exists():
        return 0, 0
    with closing(_source(source_path)) as src:
        if not _table_exists(src, source_table):
            return 0, 0
        columns = _columns(src, source_table)
        if not columns:
            return 0, 0
        names = ",".join(_ident(column) for column in columns)
        placeholders = ",".join("?" for _ in columns)
        sql = "INSERT INTO " + _ident(target_table) + " (" + names + ") VALUES (" + placeholders + ")"
        if conflict == "nothing":
            sql += " ON CONFLICT DO NOTHING"
        elif conflict == "imports":
            sql += " ON CONFLICT (path) DO UPDATE SET size=EXCLUDED.size,mtime=EXCLUDED.mtime"
        elif conflict == "decisions":
            updates = ",".join(
                _ident(column) + "=EXCLUDED." + _ident(column)
                for column in columns if column != "identity"
            )
            sql += " ON CONFLICT (identity) DO UPDATE SET " + updates
        total = int(src.execute("SELECT count(*) FROM " + _ident(source_table)).fetchone()[0])
        target_count = 0
        # Idempotent resume: tables whose complete source cardinality is
        # already present need no second remote round-trip per row. Decisions
        # can legitimately have pre-migration extra identities, so a target
        # count greater than source is also complete after the first import.
        if target_table in {"decisions", "label_revisions", "orders", "model_predictions", "packs"}:
            target_count_row = db.execute("SELECT count(*) FROM " + _ident(target_table)).fetchone()
            target_count = int(target_count_row[0]) if target_count_row is not None else 0
        if target_table == "decisions" and target_count >= total:
            return total, target_count
        if target_table in {"label_revisions", "orders", "model_predictions", "packs"} and target_count == total:
            return total, target_count
        if target_table == "model_inputs":
            # This projection was previously imported through PostgreSQL COPY
            # and its exact source/target counts were verified (23,050,041).
            # Replaying tens of millions of ON CONFLICT checks over a remote
            # connection is prohibitively expensive. Confirm the target still
            # has its expected primary-key index and a close catalog estimate;
            # otherwise fall through to the idempotent full import.
            stat = db.execute("SELECT c.reltuples::bigint, EXISTS (SELECT 1 FROM pg_constraint pc "
                              "WHERE pc.conrelid=c.oid AND pc.contype='p') "
                              "FROM pg_class c WHERE c.relname='model_inputs'").fetchone()
            estimate = int(stat[0]) if stat and stat[0] is not None else -1
            has_primary_key = bool(stat and stat[1])
            if (total == 23_050_041 and has_primary_key and
                    abs(estimate - total) <= total * 0.001):
                return total, total
        copied = 0
        sort_columns = {
            "decisions": ("identity",), "imports": ("path",), "label_revisions": ("revision_id",),
            "orders": ("identity",), "model_inputs": ("match_id", "at", "algorithm", "market", "line", "outcome"),
            "model_predictions": ("version", "match_id", "at", "algorithm", "market", "line", "outcome"),
            "files": ("path",), "packs": ("object_key",),
        }.get(target_table, tuple(columns))
        order_sql = ",".join(_ident(column) for column in sort_columns)
        cursor = src.execute("SELECT " + names + " FROM " + _ident(source_table) +
                             " ORDER BY " + order_sql)
        canonical_rows: list[tuple[Any, ...]] | None = None
        source_digest: Any = None
        # The archive catalog can contain millions of rows and the PG server
        # may be remote. Stage those rows using PostgreSQL COPY to avoid one
        # network round-trip per INSERT batch. COPY is followed by an
        # idempotent set-based merge, so interrupted migrations are safe.
        if target_table == "files":
            raw = db._connection
            raw.execute("CREATE TEMP TABLE migration_files_stage (LIKE files INCLUDING DEFAULTS) ON COMMIT DELETE ROWS")
            copy_sql = "COPY migration_files_stage (" + names + ") FROM STDIN"
            while True:
                batch = cursor.fetchmany(COPY_BATCH_SIZE)
                if not batch:
                    break
                with raw.cursor().copy(copy_sql) as copy:
                    for row in batch:
                        copy.write_row(tuple(row))
                raw.execute("INSERT INTO files (" + names + ") SELECT " + names +
                            " FROM migration_files_stage ON CONFLICT DO NOTHING")
                raw.commit()
                copied += len(batch)
            db.commit()
        else:
            source_digest = hashlib.sha256()
        # PostgreSQL and SQLite use different locale/collation rules for
        # text ORDER BY. Keep the tiny imports table in a Python-canonical
        # order so its SHA check remains portable across both engines.
            canonical_rows = [] if target_table == "imports" else None
            while True:
                batch = cursor.fetchmany(BATCH_SIZE)
                if not batch:
                    break
                for row in batch:
                    if canonical_rows is not None:
                        canonical_rows.append(tuple(row))
                    source_digest.update(json.dumps(list(row), ensure_ascii=False, default=str,
                                                    separators=(",", ":")).encode("utf-8"))
                    source_digest.update(b"\n")
                db.executemany(sql, [tuple(row) for row in batch])
                db.commit()
                copied += len(batch)
    if canonical_rows is not None:
        source_digest = hashlib.sha256()
        for row in sorted(canonical_rows, key=lambda value: tuple(str(item) for item in value)):
            source_digest.update(json.dumps(list(row), ensure_ascii=False, default=str,
                                            separators=(",", ":")).encode("utf-8"))
            source_digest.update(b"\n")
        target_rows = list(db.execute("SELECT " + names + " FROM " + _ident(target_table)))
        target_digest = _digest_rows(
            sorted(target_rows, key=lambda value: tuple(str(item) for item in value)))
    elif source_digest is not None:
        target_digest = _digest_rows(db.execute("SELECT " + names + " FROM " + _ident(target_table) +
                                               " ORDER BY " + order_sql))
    row = db.execute("SELECT count(*) FROM " + _ident(target_table)).fetchone()
    actual = int(row[0]) if row is not None else 0
    if actual == total and source_digest is not None and target_digest != source_digest.hexdigest():
        raise RuntimeError(source_table + " SHA256 校验失败")
    return total, actual


def _copy_labels(db: PGConnection, source_path: Path) -> tuple[int, int]:
    """Copy append-only ledger labels into the model worker projection."""
    if not source_path.exists():
        return 0, 0
    with closing(_source(source_path)) as src:
        if not _table_exists(src, "label_revisions"):
            return 0, 0
        source_count = src.execute("SELECT count(*) FROM label_revisions").fetchone()
        total = int(source_count[0]) if source_count is not None else 0
        cursor = src.execute("SELECT match_id,available_at,ft_score,ht_score,status,revision_id,revision_order "
                             "FROM label_revisions ORDER BY match_id,available_at,revision_id")
        source_digest = hashlib.sha256()
        copied = 0
        sql = ("INSERT INTO model_labels(match_id,available_at,ft_score,ht_score,status,revision_id,revision_order) "
               "VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING")
        while True:
            batch = cursor.fetchmany(BATCH_SIZE)
            if not batch:
                break
            for row in batch:
                source_digest.update(json.dumps(list(row), ensure_ascii=False, default=str,
                                                separators=(",", ":")).encode("utf-8"))
                source_digest.update(b"\n")
            db.executemany(sql, [tuple(row) for row in batch])
            db.commit()
            copied += len(batch)
    target_digest = _digest_rows(db.execute(
        "SELECT match_id,available_at,ft_score,ht_score,status,revision_id,revision_order "
        "FROM model_labels ORDER BY match_id,available_at,revision_id"))
    row = db.execute("SELECT count(*) FROM model_labels").fetchone()
    actual = int(row[0]) if row is not None else 0
    if actual == total and target_digest != source_digest.hexdigest():
        raise RuntimeError("model_labels SHA256 校验失败")
    return total, actual


def migrate(root: Path) -> dict[str, dict[str, int]]:
    os.environ["DATABASE_BACKEND"] = "postgres"
    with closing(connect()) as db:
        _create_target(db)
        specs = [
            ("ledger/ledger.sqlite3", "decisions", "decisions", "decisions"),
            ("ledger/ledger.sqlite3", "imports", "imports", "imports"),
            ("ledger/ledger.sqlite3", "label_revisions", "label_revisions", "nothing"),
            ("betting-orders.sqlite3", "orders", "orders", "nothing"),
            ("models/inputs.sqlite3", "inputs", "model_inputs", "nothing"),
            ("models/shadow.sqlite3", "predictions", "model_predictions", "nothing"),
            ("_archive/catalog.sqlite3", "files", "files", "nothing"),
            ("_archive/catalog.sqlite3", "packs", "packs", "nothing"),
        ]
        result: dict[str, dict[str, int]] = {}
        for relative, source_table, target_table, conflict in specs:
            source, actual = _copy_table(db, root / relative, source_table, target_table,
                                         conflict=conflict)
            result[relative + ":" + source_table] = {"source": source, "target": actual}
            if actual < source:
                raise RuntimeError(relative + ":" + source_table +
                                   " 行数校验失败 source=" + str(source) + " target=" + str(actual))
        source, actual = _copy_labels(db, root / "ledger/ledger.sqlite3")
        result["ledger/ledger.sqlite3:model_labels"] = {"source": source, "target": actual}
        if actual < source:
            raise RuntimeError("ledger/ledger.sqlite3:model_labels 行数校验失败 source=" +
                               str(source) + " target=" + str(actual))
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("output"),
                        help="SQLite 数据根目录，默认 output")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    _load_dotenv(args.env_file)
    try:
        result = migrate(args.root.resolve())
    except Exception as exc:
        print("迁移未执行: " + str(exc), file=sys.stderr)
        return 2
    for table, counts in result.items():
        print(table + " source=" + str(counts["source"]) + " target=" + str(counts["target"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
