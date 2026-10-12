#!/usr/bin/env python3
"""Verify unmanifested packs and recover their data or prove prefix coverage."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from service.model_training import atomic_json
from store.archive import S3Archive


def recover(root: Path) -> list[dict]:
    archive = S3Archive(root)
    state = root/'_archive'/'repairs'/'task42'
    with sqlite3.connect(archive.catalog) as con:
        known = {r[0] for r in con.execute('SELECT object_key FROM packs')}
    inventory = json.loads((state/'remote-inventory.json').read_text())['objects']
    orphans = [o for o in inventory if o['key'].endswith('.pack') and o['key'] not in known]
    results = []
    for item in orphans:
        key = item['key']
        folder = state/'orphans'/Path(key).stem
        folder.mkdir(parents=True,exist_ok=True)
        manifest = folder/'recovery.json'
        if manifest.exists():
            results.append(json.loads(manifest.read_text()))
            continue
        response = archive.client.get_object(Bucket=archive.bucket,Key=key,Range='bytes=0-0')
        response['Body'].close()
        expected = response.get('Metadata',{}).get('sha256')
        if not expected:
            raise OSError('孤立归档缺少原始 SHA256，不能认证恢复')
        pack = folder/'source.pack'
        digest = hashlib.sha256()
        with pack.open('wb') as target:
            for block in archive._range_blocks(key,0,item['size']):
                target.write(block)
                digest.update(block)
        if digest.hexdigest() != expected:
            raise OSError('孤立原始对象 SHA256 校验失败')
        raw = folder/'recovered.data'
        raw_hash = hashlib.sha256()
        with gzip.open(pack,'rb') as stream, raw.open('wb') as target:
            while block := stream.read(1024**2):
                target.write(block)
                raw_hash.update(block)
        with raw.open('rb') as stream:
            signature = stream.read(16)
        result = {'source_object':key,'pack_sha256':expected,'raw_sha256':raw_hash.hexdigest(),
                  'bytes':raw.stat().st_size,'status':'recovered'}
        if signature.startswith(b'SQLite format 3'):
            database = raw.with_suffix('.sqlite3')
            raw.rename(database)
            con = sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)
            result['tables'] = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            result['integrity'] = con.execute('PRAGMA integrity_check').fetchone()[0]
            con.close()
            if result['integrity'] != 'ok':
                raise OSError('恢复数据库完整性校验失败')
            result['recovered_path'] = database.relative_to(root).as_posix()
        elif signature.startswith(b'\x1f\x8b'):
            with gzip.open(raw,'rt') as stream:
                first = json.loads(next(stream))
            at = first.get('computed_at') or first.get('at')
            from datetime import datetime
            hour = datetime.fromisoformat(at).strftime('%Y%m%d%H')
            original = root/'ledger'/('live-replay.'+hour+'.jsonl.gz')
            if original.exists() and original.stat().st_size >= raw.stat().st_size:
                digest = hashlib.sha256()
                remaining = raw.stat().st_size
                with original.open('rb') as stream:
                    while remaining:
                        block = stream.read(min(remaining,1024**2))
                        if not block:
                            raise OSError('原始日志前缀不完整')
                        digest.update(block)
                        remaining -= len(block)
                if digest.hexdigest() == raw_hash.hexdigest():
                    result.update(status='verified_duplicate_prefix', covered_by=original.relative_to(root).as_posix())
                    raw.unlink()
                else:
                    result['recovered_path'] = raw.relative_to(root).as_posix()
            else:
                result['recovered_path'] = raw.relative_to(root).as_posix()
        else:
            result['recovered_path'] = raw.relative_to(root).as_posix()
        atomic_json(manifest,result)
        archive.migrate([manifest])
        results.append(result)
        pack.unlink()
        print(json.dumps(result),flush=True)
    atomic_json(state/'orphans.json',results)
    return results


if __name__ == '__main__':
    recover(Path(sys.argv[1]).resolve())
