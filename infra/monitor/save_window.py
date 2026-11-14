"""Consume private snapshot on stdin; publish metadata only, never original text."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import sys


def save(root: Path, body: bytes):
    value=json.loads(body)
    start=datetime.fromisoformat(value['start']);end=datetime.fromisoformat(value['end'])
    if end>datetime.now(timezone.utc) or end-start!=timedelta(minutes=30):
        raise ValueError('Only closed half-hour windows may be saved')
    originals=[datetime.fromisoformat(m['posted_at'])for m in value['messages']]
    earliest=min(originals,default=start)
    if earliest+timedelta(hours=72)<=datetime.now(timezone.utc):
        raise ValueError('Original-post retention elapsed')
    directory=root/str(int(start.timestamp()))
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    digest=hashlib.sha256(body).hexdigest()
    name='snapshot-'+digest+'.private.json'
    manifest={'schema_version':1,'earliest_original_posted_at':earliest.isoformat(),
              'expires_at':(earliest+timedelta(hours=72)).isoformat(),'files':[name],
              'source':'authorized Telegram window and read-only business evidence'}
    # Manifest is durable before any raw original is exported to disk.
    with (directory/(name+'.manifest.json')).open('w')as stream:
        os.fchmod(stream.fileno(),0o600);json.dump(manifest,stream);stream.flush();os.fsync(stream.fileno())
    with (directory/name).open('xb')as stream:
        os.fchmod(stream.fileno(),0o600);stream.write(body);stream.flush();os.fsync(stream.fileno())
    sources=value['source_evidence']
    summary={'start':int(start.timestamp()),'end':int(end.timestamp()),
             'snapshot_sha256':digest,'snapshot_file':str(directory/name),
             'upstream_complete':all(s.get('complete')is True for s in sources if not s.get('excluded')),
             'database_complete':value['database_complete'],
             'trusted_messages':sum(s.get('trusted_messages',0)for s in sources),
             'missing_messages':sum(s.get('missing_messages',0)for s in sources),
             'stored_versions':len(value['messages']),
             'source_count':len(sources),'excluded_sources':sum(bool(s.get('excluded'))for s in sources)}
    (directory/'snapshot-summary.json').write_text(json.dumps(summary,indent=2))
    return summary


if __name__=='__main__':
    os.umask(0o077)
    body=sys.stdin.buffer.read(32*1024*1024+1)
    if len(body)>32*1024*1024:raise ValueError('Snapshot budget exceeded')
    print(json.dumps(save(Path(os.environ['EVIDENCE_DIR']),body)))
