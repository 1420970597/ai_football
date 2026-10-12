#!/usr/bin/env python3
"""Verify every retained SQLite row before post-cutover source cleanup.

COPY stages only primary keys and SHA256 fingerprints, never a second runtime
database. PostgreSQL checks full source coverage and content in one joined scan.
Newer target ledger revisions and monotonic archive cleanup flags are permitted.
This command does not delete, alter or import production records.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import sys
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.migrate_sqlite_to_postgres import _ident, _load_dotenv
from store.postgres import connect

SPECS = (
    ('ledger/ledger.sqlite3', 'decisions', 'decisions'),
    ('ledger/ledger.sqlite3', 'label_revisions', 'label_revisions'),
    ('ledger/ledger.sqlite3', 'imports', 'imports'),
    ('betting-orders.sqlite3', 'orders', 'orders'),
    ('models/inputs.sqlite3', 'inputs', 'model_inputs'),
    ('models/shadow.sqlite3', 'predictions', 'model_predictions'),
    ('_archive/catalog.sqlite3', 'files', 'files'),
    ('_archive/catalog.sqlite3', 'packs', 'packs'),
)


def fingerprint(values: Sequence[Any], kinds: Sequence[str]) -> str:
    """Frame values identically to PostgreSQL, including exact float bits."""
    parts = []
    for value, kind in zip(values, kinds):
        if value is None:
            parts.append('-;')
        elif kind in ('REAL', 'FLOAT', 'DOUBLE'):
            parts.append('f' + struct.pack('!d', float(value)).hex() + ';')
        elif kind in ('INTEGER', 'INT', 'BIGINT'):
            parts.append('i' + str(int(value)) + ';')
        else:
            value = str(value)
            parts.append(str(len(value.encode('utf8'))) + ':' + value)
    return hashlib.sha256(''.join(parts).encode('utf8')).hexdigest()


def fingerprint_sql(columns: Sequence[str], kinds: Sequence[str]) -> str:
    parts = []
    for name, kind in zip(columns, kinds):
        col = 't.' + _ident(name)
        if kind in ('REAL', 'FLOAT', 'DOUBLE'):
            value = "'f' || encode(float8send(" + col + "::float8),'hex') || ';'"
        elif kind in ('INTEGER', 'INT', 'BIGINT'):
            value = "'i' || " + col + "::bigint::text || ';'"
        else:
            value = ("octet_length(convert_to(" + col + "::text,'UTF8'))::text || ':' || " + col + '::text')
        parts.append("CASE WHEN " + col + " IS NULL THEN '-;' ELSE " + value + ' END')
    return "encode(sha256(convert_to(" + ' || '.join('(' + p + ')' for p in parts) + ",'UTF8')),'hex')"


def verify_table(db: Any, root: Path, relative: str, source_table: str,
                 target_table: str) -> dict[str, Any]:
    path = root / relative
    if not path.exists():
        raise FileNotFoundError(path)
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
        schema = source.execute('PRAGMA table_info(' + _ident(source_table) + ')').fetchall()
        if not schema:
            raise ValueError('missing source table ' + source_table)
        keys = [r[1] for r in sorted(schema, key=lambda r: r[5]) if r[5]]
        if not keys:
            raise ValueError('source table needs a primary key')
        # cleaned is a monotonic deletion marker, imports checkpoints may
        # advance, and decisions retain the greatest committed revision.
        compare = [r for r in schema if not (target_table == 'files' and r[1] == 'cleaned')]
        columns = [r[1] for r in compare]
        kinds = [str(r[2]).upper() for r in compare]
        names = ','.join(_ident(name) for name in columns)
        key_positions = [columns.index(key) for key in keys]
        revision_position = columns.index('revision') if target_table == 'decisions' else None
        db.execute('DROP TABLE IF EXISTS migration_verify_stage')
        key_definition = ','.join(_ident(key) + ' TEXT NOT NULL' for key in keys)
        db.execute('CREATE TEMP TABLE migration_verify_stage (' + key_definition +
                   ', source_sha TEXT NOT NULL, source_revision DOUBLE PRECISION)')
        db.commit()
        raw = db._connection
        count = 0
        cursor = source.execute('SELECT ' + names + ' FROM ' + _ident(source_table))
        with raw.cursor().copy('COPY migration_verify_stage FROM STDIN') as copy:
            while True:
                batch = cursor.fetchmany(10_000)
                if not batch:
                    break
                for row in batch:
                    copy.write_row((*[str(row[i]) for i in key_positions], fingerprint(row, kinds),
                                    row[revision_position] if revision_position is not None else None))
                count += len(batch)
                if count % 500_000 == 0:
                    print(json.dumps({'table': target_table, 'staged': count}), flush=True)
        raw.commit()
        db.execute('ANALYZE migration_verify_stage')
        joins = ' AND '.join('s.' + _ident(k) + '=t.' + _ident(k) for k in keys)
        missing = 't.' + _ident(keys[0]) + ' IS NULL'
        newer = 't.revision>s.source_revision' if target_table == 'decisions' else 'FALSE'
        digest = fingerprint_sql(columns, kinds)
        mismatch = 'NOT (' + newer + ') AND ' + digest + '<>s.source_sha'
        if target_table == 'imports':
            mismatch = 'FALSE'  # Checkpoints may grow; coverage is required.
        lagging = 't.revision<s.source_revision' if target_table == 'decisions' else 'FALSE'
        query = ('SELECT count(*) AS source_rows,count(*) FILTER (WHERE ' + missing + ') AS missing_rows,'
                 'count(*) FILTER (WHERE NOT (' + missing + ') AND (' + mismatch + ')) AS content_mismatches,'
                 'count(*) FILTER (WHERE ' + lagging + ') AS lagging_revisions '
                 'FROM migration_verify_stage s LEFT JOIN ' + _ident(target_table) + ' t ON ' + joins)
        result = dict(db.execute(query).fetchone())
        result.update(source=relative, source_table=source_table, target_table=target_table,
                      staged_rows=count, verified_at=datetime.now(timezone.utc).isoformat())
        if result['source_rows'] != count or any(result[key] for key in (
                'missing_rows', 'content_mismatches', 'lagging_revisions')):
            raise ValueError('migration verification failed: ' + json.dumps(result))
        db.execute('DROP TABLE migration_verify_stage')
        db.commit()
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('output'))
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--table', choices=[s[2] for s in SPECS])
    args = parser.parse_args()
    _load_dotenv(args.env_file)
    report: dict[str, Any] = {'verified': False, 'tables': []}
    try:
        with closing(connect()) as db:
            db.execute("SET application_name='ai_football_migration_verify'")
            db.execute("SET max_parallel_workers_per_gather=0")
            db.execute("SET statement_timeout='90min'")
            for spec in SPECS:
                if args.table and spec[2] != args.table:
                    continue
                result = verify_table(db, args.root, *spec)
                report['tables'].append(result)
                args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(json.dumps(result), flush=True)
        report['verified'] = True
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        report['error'] = str(exc)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
