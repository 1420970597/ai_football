"""Prune explicitly registered exports by original post time."""
from datetime import datetime
import json
import os
from pathlib import Path
import time

def prune(root,now):
    root=Path(root).resolve(strict=True);removed=0
    manifests=set(root.rglob('*.manifest.json'))|set(root.rglob('raw-retention-manifest.json'))
    if(root/'raw-evidence-manifest.json').is_file():manifests.add(root/'raw-evidence-manifest.json')
    for manifestfile in sorted(manifests):
        if manifestfile.is_symlink():raise ValueError('manifest_symlink')
        value=json.loads(manifestfile.read_text())
        if 'expires_at'not in value:continue
        expires=datetime.fromisoformat(value['expires_at'])
        if expires.tzinfo is None:raise ValueError('timezone_required')
        # Legacy image exports recorded an absolute original-based expiry
        # without repeating posted_at. Honor that existing cutoff; never
        # manufacture a new timestamp or extend it by the export time.
        if value.get('earliest_original_posted_at'):
            posted=datetime.fromisoformat(value['earliest_original_posted_at'])
            if posted.tzinfo is None:raise ValueError('timezone_required')
            if not 0<=expires.timestamp()-posted.timestamp()<=72*3600:raise ValueError('invalid_original_ttl')
        if not isinstance(value.get('files'),list):raise ValueError('invalid_export_files')
        paths=[]
        for name in value['files']:
            if not isinstance(name,str)or Path(name).name!=name or name in {'','.','..'}:raise ValueError('export_path_escape')
            path=manifestfile.parent/name
            if path.is_symlink()or not path.resolve().is_relative_to(root):raise ValueError('export_symlink')
            paths.append(path)
        if expires.timestamp()>now:continue
        for path in paths:
            if path.is_file():path.unlink();removed+=1
        manifestfile.unlink()
    return removed

if __name__=='__main__':
    os.umask(0o077)
    while True:
        try:print(json.dumps({'removed':prune(os.environ['PRIVATE_EVIDENCE_ROOT'],time.time())}),flush=True)
        except Exception as exc:print(json.dumps({'retention_error':type(exc).__name__}),flush=True)
        time.sleep(30)
