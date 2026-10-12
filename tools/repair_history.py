#!/usr/bin/env python3
"""Resumable local/S3 historical remediation. Original remote packs are immutable."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service.history_repair import REPAIR_VERSION, merge_final_evidence, repair_grade, repair_replay
from service.model_training import atomic_json
from store.archive import S3Archive
from store.training_data import labels, snapshot_inputs, write_inputs


def backup(source: Path, target: Path) -> None:
    if target.exists() or not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)


def inventory(archive: S3Archive, state: Path) -> sqlite3.Connection:
    frozen = state / 'catalog.sqlite3'
    backup(archive.catalog, frozen)
    db = sqlite3.connect(state / 'progress.sqlite3')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY,record TEXT,status TEXT,stats TEXT)')
    with closing(sqlite3.connect(frozen)) as src:
        src.row_factory = sqlite3.Row
        rows = src.execute("SELECT * FROM files WHERE path LIKE 'ledger/live-replay%' OR "
                           "(path LIKE 'ledger/ledger%' AND path LIKE '%.jsonl') OR "
                           "path LIKE '_live/score-history%' OR path LIKE 'models/%.json'")
        with db:
            db.executemany('INSERT OR IGNORE INTO sources VALUES (?,?,?,?)',
                           ((r['path'], json.dumps(dict(r)), 'pending', '{}') for r in rows))
    return db


def source_file(archive: S3Archive, record: dict[str, Any], target: Path) -> Path:
    local = archive.root / record['path']
    if local.exists():
        stat = local.stat()
        if stat.st_size == record['size'] and stat.st_mtime_ns == record['mtime_ns']:
            with local.open('rb') as f:
                if hashlib.file_digest(f, 'sha256').hexdigest() == record['sha256']:
                    return local
    archive.copy_verified(record, target)
    return target


def install_local_revision(root: Path, state: Path, local: Path, staged: Path,
                           record: dict[str, Any], *, replay: bool) -> str:
    if not local.exists():
        staged.unlink(missing_ok=True)
        return 'remote_only'
    stat = local.stat()
    if (stat.st_size, stat.st_mtime_ns) != (record['size'], record['mtime_ns']):
        pending = state / 'pending-local' / local.name
        pending.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, pending)
        return 'pending_local_tail_merge'
    if not replay:
        os.replace(staged, local)
        return 'installed'

    # Serialize gzip replacement with the model worker's incremental reader.
    lock_path = root / 'models' / 'replay.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            current = local.stat()
            if (current.st_size, current.st_mtime_ns) != (record['size'], record['mtime_ns']):
                pending = state / 'pending-local' / local.name
                pending.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, pending)
                return 'pending_local_tail_merge'
            os.replace(staged, local)
            cursor_path = root / 'models' / 'replay-cursor.json'
            cursor = json.loads(cursor_path.read_text()) if cursor_path.exists() else {}
            cursor[local.name] = local.stat().st_size
            atomic_json(cursor_path, cursor)
            return 'installed'
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run(root: Path, state: Path, backup_root: Path) -> None:
    state.mkdir(parents=True, exist_ok=True)
    archive = S3Archive(root)
    db = inventory(archive, state)
    backup(root / 'models' / 'inputs.sqlite3', backup_root / 'inputs.sqlite3')
    backup(root / 'models' / 'shadow.sqlite3', backup_root / 'shadow.sqlite3')
    backup(root / 'ledger' / 'ledger.sqlite3', backup_root / 'ledger.sqlite3')
    try:
        import orjson  # pyright: ignore[reportMissingImports]
        loads: Callable[[Any], Any] = orjson.loads
        dumps: Callable[[Any], bytes] = orjson.dumps
    except ImportError:
        loads = json.loads
        def _fallback_dumps(r: Any) -> bytes:
            return json.dumps(r, ensure_ascii=False, separators=(',', ':')).encode()
        dumps = _fallback_dumps
    with closing(sqlite3.connect(root / 'ledger' / 'ledger.sqlite3')) as ledger:
        finals = labels(ledger)
    # The score journal is the permanent evidence, including finals no longer cached.
    score_events = []
    for r in db.execute("SELECT * FROM sources WHERE path LIKE '_live/score-history%' ORDER BY path").fetchall():
        record = json.loads(r['record'])
        source = source_file(archive, record, state / 'source')
        with gzip.open(source, 'rb') as score_stream:
            for line in score_stream:
                row = loads(line)
                score_events.append(row)
        if source == state / 'source':
            source.unlink()
        with db:
            db.execute("UPDATE sources SET status='evidence',stats='{}' WHERE path=?", (r['path'],))
    finals = merge_final_evidence(finals,score_events)
    del score_events
    atomic_json(state / 'finals.json', finals)
    last_path = ''
    sources = db.execute("SELECT * FROM sources WHERE status='pending' AND path LIKE 'ledger/%' "
                         "ORDER BY path LIKE '%.gz' DESC,path").fetchall()
    for r in sources:
        last_path = r['path']
        record = json.loads(r['record'])
        counts: Counter[str] = Counter()
        source = source_file(archive, record, state / 'source')
        replay = 'live-replay' in r['path']
        decision = r['path'].startswith('ledger/ledger')
        compressed = r['path'].endswith('.gz')
        opener = gzip.open if compressed else open
        staged = state / ('revision.jsonl.gz' if compressed else 'revision.jsonl')
        inputs = []
        started = time.monotonic()
        writer = gzip.open(staged, 'wb', compresslevel=1) if compressed else staged.open('wb')
        with opener(source, 'rb') as stream, writer as target:
            for no, line in enumerate(stream, 1):
                row = loads(line)
                if replay:
                    revised = repair_replay(row, finals)
                elif decision:
                    revised = repair_grade(row, finals)
                else:
                    # LLM review journals are audit evidence and do not have
                    # the decision schema expected by repair_grade().
                    revised = dict(row)
                    revised.setdefault('repair_status', 'preserved_non_decision_archive')
                counts[revised['repair_status']] += 1
                counts['records'] += 1
                revised['repair_source'] = {'object_key':record['object_key'], 'sha256':record['sha256'], 'record':no}
                target.write(dumps(revised) + b'\n')
                if replay:
                    batch = snapshot_inputs(revised)
                    inputs.extend(batch)
                    counts['rebuilt_inputs'] += len(batch)
                    if len(inputs) >= 1000:
                        write_inputs(root / 'models' / 'inputs.sqlite3', inputs, repair=True)
                        inputs.clear()
                if no % 5000 == 0:
                    print(json.dumps({'phase':'processing','path':r['path'],**counts,'seconds':round(time.monotonic()-started)}), flush=True)
        if inputs:
            write_inputs(root / 'models' / 'inputs.sqlite3', inputs, repair=True)
        os.utime(staged, ns=(time.time_ns(), time.time_ns()))
        # New permanent manifest makes rebuild_catalog recover the repaired view.
        archive.revise(root / r['path'], staged)
        local = root / r['path']
        local_status = install_local_revision(root, state, local, staged, record, replay=replay)
        if local_status == 'pending_local_tail_merge':
            counts[local_status] += 1
        if source == state / 'source':
            source.unlink()
        with db:
            db.execute("UPDATE sources SET status='repaired',stats=? WHERE path=?", (json.dumps(dict(counts)),r['path']))
        totals: Counter[str] = Counter()
        for (stats,) in db.execute("SELECT stats FROM sources WHERE status='repaired'"):
            totals.update(json.loads(stats))
        progress = {'version':REPAIR_VERSION, 'phase':'repairing', 'totals':dict(totals),
                    'files':db.execute("SELECT count(*) FROM sources WHERE status='repaired'").fetchone()[0],
                    'remaining_files':db.execute("SELECT count(*) FROM sources WHERE status='pending'").fetchone()[0],
                    'last_path':r['path'], 'final_matches':len(finals)}
        atomic_json(state / 'status.json', progress)
        print(json.dumps(progress), flush=True)
    # Old models are evidence only; version guards exclude them from inference.
    for r in db.execute("SELECT * FROM sources WHERE status='pending' AND path LIKE 'models/%'").fetchall():
        with db:
            db.execute("UPDATE sources SET status='quarantined_model_evidence' WHERE path=?", (r['path'],))
    totals = Counter()
    for (stats,) in db.execute("SELECT stats FROM sources WHERE status='repaired'"):
        totals.update(json.loads(stats))
    repaired_files = db.execute("SELECT count(*) FROM sources WHERE status='repaired'").fetchone()[0]
    pending_files = db.execute("SELECT count(*) FROM sources WHERE status='pending'").fetchone()[0]
    quarantined_models = db.execute(
        "SELECT count(*) FROM sources WHERE status='quarantined_model_evidence'").fetchone()[0]
    # Publish a terminal phase after model evidence is quarantined so the
    # dashboard stops presenting the system as still restoring.
    atomic_json(state / 'status.json', {
        'version': REPAIR_VERSION, 'phase': 'completed', 'totals': dict(totals),
        'files': repaired_files, 'remaining_files': pending_files,
        'quarantined_model_evidence': quarantined_models,
        'last_path': last_path, 'final_matches': len(finals),
    })
    atomic_json(state / 'completion.json', {'version':REPAIR_VERSION,
                'counts':dict(db.execute('SELECT status,count(*) FROM sources GROUP BY status').fetchall()),
                'unresolved_are_explicit':True, 'original_packs_retained':True})
    # Store frozen inventory/progress remotely too; credentials are never included.
    archive.migrate([state / 'catalog.sqlite3', state / 'progress.sqlite3', state / 'finals.json',
                     state / 'status.json', state / 'completion.json'])
    db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    run(args.root.resolve(), args.root.resolve() / '_archive' / 'repairs' / 'task42', args.backup.resolve())
