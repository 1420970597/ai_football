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
import os
from pathlib import Path
import re
import sqlite3
import struct
import sys
import time
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
RESUMABLE_VERSION = 'full-row-sha256-indexed-v1'
COPY_BATCH_ROWS = 100_000
VERIFY_BATCH_ROWS = 20_000
QUERY_RETRIES = 2
SESSION_WORK_MEM = '256MB'


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


def fingerprint_sql(columns: Sequence[str], kinds: Sequence[str], *, binary: bool = False) -> str:
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
    digest = "sha256(convert_to(" + ' || '.join('(' + p + ')' for p in parts) + ",'UTF8'))"
    return digest if binary else "encode(" + digest + ",'hex')"


def _source_signature(path: Path, schema: Sequence[Any]) -> dict[str, Any]:
    """Pin the source file, WAL and table schema without another file scan."""
    files: dict[str, Any] = {}
    for suffix in ('', '-wal'):
        candidate = Path(str(path.resolve())+suffix)
        if candidate.exists():
            stat = candidate.stat()
            files[suffix or 'database'] = dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                ctime_ns=stat.st_ctime_ns, device=stat.st_dev, inode=stat.st_ino)
    return dict(path=str(path.resolve()), files=files,
                schema_sha256=hashlib.sha256(json.dumps(list(schema), ensure_ascii=False).encode()).hexdigest(),
                verifier_version=RESUMABLE_VERSION)


def _assert_source(path: Path, schema: Sequence[Any], signature: dict[str, Any]) -> None:
    if _source_signature(path, schema) != signature:
        raise ValueError('源文件或WAL已变化，保留stage并拒绝续验；请使用固定备份和新的run-id')


def _reconcile_metadata(path: Path, relative: str, source_table: str,
                        schema: Sequence[Any], state: dict[str, Any],
                        signature: dict[str, Any], evidence_path: Path) -> dict[str, Any]:
    """Re-pin ctime only after hashing every byte against a verified backup.

    SQLite can chmod WAL/SHM while opening a read-only database. Never treat
    unchanged mtime as sufficient evidence: source DB and WAL both require
    complete byte hashes from an independently verified fixed backup.
    """
    previous = state['source_signature']
    def without_ctime(value: dict[str, Any]) -> dict[str, Any]:
        return {**value, 'files': {key: {name: item for name, item in info.items()
                    if name != 'ctime_ns'} for key, info in value['files'].items()}}
    if without_ctime(previous) != without_ctime(signature):
        raise ValueError('metadata-only恢复只允许ctime变化，源路径/schema/大小/mtime/inode必须相同')
    evidence_bytes = evidence_path.read_bytes()
    evidence = json.loads(evidence_bytes)
    if (evidence.get('verified') is not True or
            evidence.get('archive_sha256') != evidence.get('archive_sha256_expected') or
            not re.fullmatch(r'[0-9a-f]{64}', str(evidence.get('archive_sha256', '')))):
        raise ValueError('固定备份完整字节校验证据未通过')
    table = evidence.get('tables', {}).get(relative+':'+source_table, {})
    if (table.get('readable') is not True or table.get('schema_matches_source') is not True or
            table.get('expected_rows') != state['source_rows']):
        raise ValueError('备份表的schema/完整源数量证据不匹配')
    hashes: dict[str, str] = {}
    for key, info in signature['files'].items():
        suffix = '' if key == 'database' else key
        member = evidence.get('members', {}).get(relative+suffix, {})
        expected = member.get('archive_sha256')
        if (member.get('byte_identical') is not True or member.get('bytes') != info['size'] or
                expected != member.get('extracted_sha256') or
                not re.fullmatch(r'[0-9a-f]{64}', str(expected or ''))):
            raise ValueError('固定备份成员缺少完整字节证据: '+relative+suffix)
        with Path(str(path.resolve())+suffix).open('rb') as source:
            actual = hashlib.file_digest(source, 'sha256').hexdigest()
        if actual != expected:
            raise ValueError('当前源与固定备份完整SHA256不一致: '+relative+suffix)
        hashes[key] = actual
        print(json.dumps({'metadata_only_source_sha256': relative+suffix,
                          'bytes': info['size'], 'sha256': actual}), flush=True)
    _assert_source(path, schema, signature)
    audit = {'at': datetime.now(timezone.utc).isoformat(), 'old_signature': previous,
        'new_signature': signature, 'complete_sha256': hashes,
        'evidence_path': str(evidence_path.resolve()),
        'evidence_sha256': hashlib.sha256(evidence_bytes).hexdigest(),
        'archive_sha256': evidence['archive_sha256']}
    return {**state, 'source_signature': signature,
            'metadata_only_resume_audit': [*state.get('metadata_only_resume_audit', []), audit]}


def _stage_names(path: Path, target: str, run_id: str | None) -> tuple[str, str, str]:
    # Identifiers never contain user text, paths or credentials. The stable
    # default deliberately excludes file mtime so changed sources are rejected.
    identity = run_id or str(path.resolve())+'\0'+target
    token = hashlib.sha256((identity+'\0'+target).encode()).hexdigest()[:24]
    stage = 'migration_verify_'+token
    return stage, stage+'_state', token


def _save_state(db: Any, metadata: str, state: dict[str, Any]) -> None:
    db.execute('INSERT INTO '+_ident(metadata)+'(id,payload) VALUES (1,?) '
        'ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
        (json.dumps(state, ensure_ascii=False, allow_nan=False),)).close()


def _copy_stage_batch(db: Any, stage: str, metadata: str, rows: Sequence[Sequence[Any]],
                      state: dict[str, Any], path: Path, schema: Sequence[Any]) -> dict[str, Any]:
    next_state = {**state, 'copied_rows': state['copied_rows']+len(rows),
                  'copy_after': int(rows[-1][0])}
    with db:
        with db._connection.cursor() as cursor:
            with cursor.copy('COPY '+_ident(stage)+' FROM STDIN') as copy:
                for row in rows:
                    copy.write_row(row)
        _assert_source(path, schema, state['source_signature'])
        # Rows and their source ROWID checkpoint commit together. A connection
        # failure rolls both back, so a resumed COPY cannot skip or duplicate.
        _save_state(db, metadata, next_state)
    return next_state


def _bounded_verify_sql(stage: str, target: str, keys: Sequence[str], columns: Sequence[str],
                        kinds: Sequence[str]) -> str:
    joins = ' AND '.join('s.'+_ident(key)+'=t.'+_ident(key) for key in keys)
    digest = fingerprint_sql(columns, kinds, binary=True)
    revision = 't.revision' if target == 'decisions' else 'NULL::double precision'
    newer = 't.target_revision>s.source_revision' if target == 'decisions' else 'FALSE'
    lagging = 't.target_revision<s.source_revision' if target == 'decisions' else 'FALSE'
    mismatch = 'NOT ('+newer+') AND t.target_sha<>s.source_sha'
    if target == 'imports':
        mismatch = 'FALSE'
    # OFFSET 0 prevents flattening the correlated target subquery into a full
    # relation hash join. Full unique keys bound the target lookup to one row;
    # target_sha is computed before that row reaches the aggregate.
    return ('WITH batch AS MATERIALIZED (SELECT * FROM '+_ident(stage)+
        ' WHERE (?::bigint IS NULL OR source_rowid>?) ORDER BY source_rowid LIMIT ?) '
        'SELECT count(*) AS source_rows,max(s.source_rowid) AS last_rowid,'
        'count(*) FILTER (WHERE t.found IS NULL) AS missing_rows,'
        'count(*) FILTER (WHERE t.found IS NOT NULL AND ('+mismatch+')) AS content_mismatches,'
        'count(*) FILTER (WHERE '+lagging+') AS lagging_revisions '
        'FROM batch s LEFT JOIN LATERAL (SELECT TRUE AS found,'+digest+
        ' AS target_sha,'+revision+' AS target_revision FROM '+_ident(target)+
        ' t WHERE '+joins+' OFFSET 0) t ON TRUE')


def _require_target_index(plan: Any, target: str, keys: Sequence[str] = ()) -> None:
    """Reject a plan that scans the target heap for each bounded source row."""
    if isinstance(plan, str):
        plan = json.loads(plan)
    scans: list[dict[str, Any]] = []
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get('Relation Name') == target:
                scans.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(plan)
    if not scans or any(node.get('Node Type') not in ('Index Scan', 'Index Only Scan') or
                        not node.get('Index Cond') or any(
                            not re.search(r'(?<![A-Za-z0-9_])'+re.escape(key)+r'(?![A-Za-z0-9_])',
                                          str(node['Index Cond'])) for key in keys) for node in scans):
        raise ValueError('核验目标必须使用完整主键索引查找，拒绝全表计划；stage保留')


def _query_retry(db: Any, sql: str, params: Sequence[Any], *, retries: int) -> dict[str, Any]:
    for attempt in range(retries+1):
        try:
            row = db.execute(sql, params).fetchone()
            if row is None:
                raise ValueError('核验结果缺失')
            return dict(row)
        except Exception as exc:
            db.rollback()
            # Only a statement cancellation/timeout is retried on the same
            # live connection. Broken sessions resume through persistent stage.
            if getattr(exc, 'sqlstate', None) != '57014' or attempt == retries:
                raise
            time.sleep(min(1.0, .25*(attempt+1)))
    raise RuntimeError('unreachable retry state')


def verify_resumable(db: Any, root: Path, relative: str, source_table: str, target: str,
                     *, run_id: str | None = None, copy_batch: int = COPY_BATCH_ROWS,
                     verify_batch: int = VERIFY_BATCH_ROWS, retries: int = QUERY_RETRIES,
                     metadata_only_evidence: Path | None = None) -> dict[str, Any]:
    """Verify every source row with crash-safe checkpoints and bounded lookup.

    UNLOGGED stage/state survive client disconnects, but PG recovery may erase
    them. Both are unlogged so recovery never leaves a verified checkpoint over
    lost stage rows. Metadata mismatch or missing staged coverage fails closed.
    Successful stage is retained until the CLI persists its full report.
    """
    if not 1 <= copy_batch <= 100_000 or not 1 <= verify_batch <= 100_000 or not 0 <= retries <= 3:
        raise ValueError('批次必须在1~100000之间，重试必须在0~3之间')
    path = root/relative
    stage, metadata, token = _stage_names(path, target, run_id)
    locked = db.execute('SELECT pg_try_advisory_lock(hashtext(?))', (stage,)).fetchone()
    if locked is None or not locked[0]:
        raise ValueError('相同run-id已有核验进程运行')
    try:
        db.execute("SET work_mem='"+SESSION_WORK_MEM+"'").close()
        db.execute('SET max_parallel_workers_per_gather=0').close()
        db.execute('SET effective_io_concurrency=32').close()
        db.execute("SET statement_timeout='60s'").close()
        db.execute("SET lock_timeout='3s'").close()
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as source:
            source.execute('BEGIN')
            schema = source.execute('PRAGMA table_info('+_ident(source_table)+')').fetchall()
            if not schema:
                raise ValueError('missing source table '+source_table)
            schema_sql = source.execute('SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? '
                                        'ORDER BY type,name', (source_table,)).fetchall()
            schema_identity = [schema, schema_sql]
            signature = _source_signature(path, schema_identity)
            keys = [str(row[1]) for row in sorted(schema, key=lambda row: row[5]) if row[5]]
            if not keys or any(str(row[1]).casefold() in ('_rowid_', 'source_rowid', 'source_sha', 'source_revision')
                               for row in schema):
                raise ValueError('可恢复核验需要无_rowid_遮蔽的ROWID表和主键')
            # WITHOUT ROWID tables fail this read before any stage is written.
            first = source.execute('SELECT _rowid_ FROM '+_ident(source_table)+' ORDER BY _rowid_ LIMIT 1').fetchone()
            total = int(source.execute('SELECT count(*) FROM '+_ident(source_table)).fetchone()[0])
            _assert_source(path, schema_identity, signature)
            compare = [row for row in schema if not (target == 'files' and row[1] == 'cleaned')]
            columns = [str(row[1]) for row in compare]
            kinds = [str(row[2]).upper() for row in compare]
            key_positions = [columns.index(key)+1 for key in keys]
            revision_position = columns.index('revision')+1 if target == 'decisions' else None
            with db:
                db.execute('CREATE UNLOGGED TABLE IF NOT EXISTS '+_ident(metadata)+
                           '(id integer PRIMARY KEY,payload text NOT NULL)').close()
                definitions = ','.join(_ident(key)+' TEXT NOT NULL' for key in keys)
                db.execute('CREATE UNLOGGED TABLE IF NOT EXISTS '+_ident(stage)+
                    '(source_rowid BIGINT PRIMARY KEY,'+definitions+
                    ',source_sha BYTEA NOT NULL,source_revision DOUBLE PRECISION)').close()
                previous = db.execute('SELECT payload FROM '+_ident(metadata)+' WHERE id=1').fetchone()
                if previous is not None:
                    state = json.loads(previous[0])
                    if (state.get('source_rows') != total or
                            state.get('target_table') != target or state.get('source_table') != source_table):
                        raise ValueError('源身份/数据量与已有stage不一致，拒绝续验')
                    if state.get('source_signature') != signature:
                        if metadata_only_evidence is None:
                            raise ValueError('源身份/数据量与已有stage不一致，拒绝续验')
                        state = _reconcile_metadata(path, relative, source_table, schema_identity,
                                                    state, signature, metadata_only_evidence)
                        _save_state(db, metadata, state)
                else:
                    orphan = db.execute('SELECT 1 FROM '+_ident(stage)+' LIMIT 1').fetchone()
                    if orphan:
                        raise ValueError('stage缺少恢复元数据，拒绝使用孤立数据')
                    state = dict(source_signature=signature, source_rows=total, source_table=source_table,
                        target_table=target, copied_rows=0, copy_after=None, verified_rows=0,
                        verify_after=None, copy_complete=False, complete=False, missing_rows=0,
                        content_mismatches=0, lagging_revisions=0, run_id=token,
                        started_at=datetime.now(timezone.utc).isoformat())
                    _save_state(db, metadata, state)
            if state['copy_after'] is not None:
                tail = db.execute('SELECT source_rowid FROM '+_ident(stage)+' WHERE source_rowid=?',
                                   (state['copy_after'],)).fetchone()
                if tail is None:
                    raise ValueError('恢复stage丢失已提交数据，拒绝跳过COPY')
            names = ','.join(_ident(name) for name in columns)
            while not state['copy_complete']:
                _assert_source(path, schema_identity, signature)
                after = state['copy_after']
                sql = 'SELECT _rowid_,'+names+' FROM '+_ident(source_table)
                params: tuple[Any, ...] = ()
                if after is not None:
                    sql += ' WHERE _rowid_>?'
                    params = (after,)
                batch = source.execute(sql+' ORDER BY _rowid_ LIMIT ?', (*params, copy_batch)).fetchall()
                if not batch:
                    if state['copied_rows'] != total:
                        raise ValueError('COPY源覆盖数量不一致')
                    state = {**state, 'copy_complete': True}
                    with db:
                        _save_state(db, metadata, state)
                    break
                framed = []
                for row in batch:
                    if any(row[i] is None for i in key_positions):
                        raise ValueError('源主键存在NULL，无法确保唯一覆盖')
                    framed.append((int(row[0]), *[str(row[i]) for i in key_positions],
                        bytes.fromhex(fingerprint(row[1:], kinds)),
                        row[revision_position] if revision_position is not None else None))
                state = _copy_stage_batch(db, stage, metadata, framed, state, path, schema_identity)
                print(json.dumps({'table': target, 'run_id': token, 'staged': state['copied_rows']}), flush=True)
            query = _bounded_verify_sql(stage, target, keys, columns, kinds)
            # EXPLAIN does not execute the target lookup or scan any rows.
            if total:
                row = db.execute('EXPLAIN (FORMAT JSON) '+query, (None, None, verify_batch)).fetchone()
                if row is None:
                    raise ValueError('核验执行计划缺失')
                _require_target_index(row[0], target, keys)
            if first is None and total != 0:
                raise ValueError('源ROWID范围不一致')
            while not state['complete']:
                _assert_source(path, schema_identity, signature)
                result = _query_retry(db, query, (state['verify_after'], state['verify_after'], verify_batch), retries=retries)
                count = int(result['source_rows'])
                if count == 0:
                    if state['verified_rows'] != total or state['copied_rows'] != total:
                        raise ValueError('完整源覆盖核验失败')
                    if any(state[name] for name in ('missing_rows', 'content_mismatches', 'lagging_revisions')):
                        raise ValueError('完整内容核验失败，stage与计数保留: '+json.dumps(state))
                    _assert_source(path, schema_identity, signature)
                    state = {**state, 'complete': True, 'verified_at': datetime.now(timezone.utc).isoformat()}
                    with db:
                        _save_state(db, metadata, state)
                    break
                last = int(result['last_rowid'])
                if state['verify_after'] is not None and last <= state['verify_after']:
                    raise ValueError('核验游标未前进')
                state = {**state, 'verify_after': last, 'verified_rows': state['verified_rows']+count,
                    **{name: state[name]+int(result[name]) for name in
                       ('missing_rows', 'content_mismatches', 'lagging_revisions')}}
                with db:
                    _assert_source(path, schema_identity, signature)
                    _save_state(db, metadata, state)
                print(json.dumps({'table': target, 'run_id': token, 'verified_rows': state['verified_rows'],
                    'missing_rows': state['missing_rows'], 'content_mismatches': state['content_mismatches']}), flush=True)
            _assert_source(path, schema_identity, signature)
            return {**state, 'source': relative, 'staged_rows': state['copied_rows'], 'resumable': True,
                    'stage': stage, 'metadata': metadata, 'method': RESUMABLE_VERSION,
                    'copy_batch_rows': copy_batch, 'verify_batch_rows': verify_batch,
                    'query_retries': retries, 'session_work_mem': SESSION_WORK_MEM}
    finally:
        try:
            db.rollback()
            db.execute('SELECT pg_advisory_unlock(hashtext(?))', (stage,)).fetchone()
        except Exception:
            pass  # A lost session releases its advisory lock automatically.


def cleanup_resumable(db: Any, result: dict[str, Any]) -> None:
    """Called only after the full successful result has been written to disk."""
    if not result.get('complete') or not result.get('resumable'):
        raise ValueError('未完成的stage不得清理')
    stage = result['stage']
    locked = db.execute('SELECT pg_try_advisory_lock(hashtext(?))', (stage,)).fetchone()
    if locked is None or not locked[0]:
        raise ValueError('stage正在被另一核验会话使用，保留等待清理')
    try:
        with db:
            db.execute('DROP TABLE '+_ident(stage)).close()
            db.execute('DROP TABLE '+_ident(result['metadata'])).close()
    finally:
        db.execute('SELECT pg_advisory_unlock(hashtext(?))', (stage,)).fetchone()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    """Persist completion evidence before dropping its recoverable stage."""
    temporary = path.with_name(path.name+'.tmp')
    with temporary.open('w', encoding='utf-8') as destination:
        json.dump(report, destination, ensure_ascii=False, indent=2, allow_nan=False)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


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
    parser.add_argument('--resumable', action='store_true', help='UNLOGGED stage和有界主键核验，可断线续验')
    parser.add_argument('--run-id', help='续验身份；同一源备份与目标必须保持一致')
    parser.add_argument('--copy-batch', type=int, default=COPY_BATCH_ROWS)
    parser.add_argument('--verify-batch', type=int, default=VERIFY_BATCH_ROWS)
    parser.add_argument('--query-retries', type=int, default=QUERY_RETRIES)
    parser.add_argument('--metadata-only-evidence', type=Path,
                        help='已验证固定备份的完整字节报告；只允许ctime变化并重新完整SHA核对')
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
                result = (verify_resumable(db, args.root, *spec, run_id=args.run_id,
                    copy_batch=args.copy_batch, verify_batch=args.verify_batch, retries=args.query_retries,
                    metadata_only_evidence=args.metadata_only_evidence)
                    if args.resumable else verify_table(db, args.root, *spec))
                report['tables'].append(result)
                _write_report(args.report, report)
                print(json.dumps(result), flush=True)
                if args.resumable:
                    cleanup_resumable(db, result)
        report['verified'] = True
        _write_report(args.report, report)
        return 0
    except Exception as exc:
        report['error'] = str(exc)
        _write_report(args.report, report)
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
