#!/usr/bin/env python3
"""Remediate current projections and old database backups using proven inputs."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.learned_model import DATA_VERSION
from core.live_model import MODEL_VERSION
from service.history_repair import repair_grade
from store.ledger_db import FIELDS, LedgerDB
from store.training_data import migrate_basis_columns


def column(con: sqlite3.Connection, table: str, name: str, kind: str) -> None:
    if name not in {r[1] for r in con.execute('PRAGMA table_info('+table+')')}:
        con.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+kind)


def vectors(path: Path, authority: Path, table: str) -> dict:
    with closing(sqlite3.connect(path,timeout=30)) as con,con:
        con.execute('PRAGMA journal_mode=WAL')
        migrate_basis_columns(con,table)
        column(con,table,'repair_status','TEXT')
        column(con,table,'original_x_sha256','TEXT')
        con.create_function('sha256',1,lambda x: hashlib.sha256(x.encode()).hexdigest() if x else None)
        con.execute('UPDATE '+table+' SET original_x_sha256=sha256(x) WHERE original_x_sha256 IS NULL')
        if path.resolve() != authority.resolve():
            con.execute('ATTACH DATABASE ? AS clean',(str(authority.resolve()),))
            where = ' AND '.join('s.'+k+'='+table+'.'+k for k in ('match_id','at','algorithm','market','line','outcome'))
            where += ' AND s.data_version=? AND s.model_version=?'
            names = ('x','entry_score','settlement_basis','model_version','data_version')
            assignments = ','.join(k+'=(SELECT s.'+k+' FROM clean.inputs s WHERE '+where+')' for k in names)
            parameters = [v for _ in names for v in (DATA_VERSION,MODEL_VERSION)]
            con.execute('UPDATE '+table+' SET '+assignments+' WHERE EXISTS(SELECT 1 FROM clean.inputs s WHERE '+where+')',
                        parameters+[DATA_VERSION,MODEL_VERSION])
        con.execute('UPDATE '+table+" SET x=NULL,repair_status='missing_frozen_input_snapshot' "
                    'WHERE data_version IS NOT ? OR model_version IS NOT ?',(DATA_VERSION,MODEL_VERSION))
        con.execute('UPDATE '+table+" SET repair_status='rebuilt_verified_snapshot' WHERE x IS NOT NULL "
                    'AND data_version=? AND model_version=?',(DATA_VERSION,MODEL_VERSION))
        if table == 'predictions':
            column(con,table,'predicted_at','TEXT')
            column(con,table,'original_probability','REAL')
            con.execute('UPDATE predictions SET original_probability=coalesce(original_probability,probability),'
                        "probability=NULL,repair_status='retired_model_'||repair_status")
        return {'table':table,'rows':con.execute('SELECT count(*) FROM '+table).fetchone()[0],
                'states':dict(con.execute('SELECT repair_status,count(*) FROM '+table+' GROUP BY repair_status').fetchall())}


def ledger(path: Path, finals: dict) -> dict:
    db = LedgerDB(path)
    counts: Counter[str] = Counter()
    cursor = 0
    stamp = datetime.now(timezone.utc).isoformat()
    try:
        while True:
            batch = db.connection.execute('SELECT rowid,identity,payload FROM decisions WHERE rowid>? ORDER BY rowid LIMIT 256',(cursor,)).fetchall()
            if not batch:
                break
            with db.connection:
                for rid, identity, payload in batch:
                    cursor = rid
                    row = json.loads(payload)
                    out = repair_grade(row,finals)
                    counts[out['repair_status']] += 1
                    out['updated_at'] = stamp
                    names = [k for k in FIELDS if k in out]
                    db.connection.execute('UPDATE decisions SET payload=?,revision=?,'+
                        ','.join(k+'=?' for k in names)+' WHERE identity=?',
                        [json.dumps(out,ensure_ascii=False),datetime.fromisoformat(stamp).timestamp(),
                         *[out[k] for k in names],identity])
        return {'table':'decisions','states':dict(counts),'rows':sum(counts.values())}
    finally:
        db.connection.close()


def repair(path: Path, authority: Path, finals: dict) -> dict:
    with closing(sqlite3.connect(path)) as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'inputs' in tables:
        return vectors(path,authority,'inputs')
    if 'predictions' in tables:
        return vectors(path,authority,'predictions')
    if 'decisions' in tables:
        return ledger(path,finals)
    if 'orders' in tables:
        return {'table':'orders','status':'actual_provider_receipts_preserved'}
    raise ValueError('未知历史数据库类型')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--authority',type=Path,required=True)
    parser.add_argument('--finals',type=Path,required=True)
    parser.add_argument('databases',type=Path,nargs='+')
    args = parser.parse_args()
    finals = json.loads(args.finals.read_text())
    for path in args.databases:
        print(json.dumps({'path':str(path),**repair(path,args.authority,finals)},ensure_ascii=False),flush=True)
