"""One constrained training worker; reads the indexed projection, never replay archives."""
from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import resource
import sys
from datetime import datetime, timezone
from typing import Any

from core.learned_model import train
from store.training_data import dataset


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def constrain(cpu: int, memory_mb: int) -> dict[str, Any]:
    allowed = sorted(os.sched_getaffinity(0))
    os.sched_setaffinity(0, allowed[:cpu])
    memory = memory_mb * 1024**2
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (600, 600))
    os.nice(10)
    return {'cpu_cores': len(os.sched_getaffinity(0)), 'memory_mb': memory_mb, 'cpu_time_limit_s': 600}


def run(root: Path, cycle: int, cpu: int, memory: int) -> dict[str, Any]:
    limits = constrain(cpu, memory)
    model_root = root.parent / 'models'
    active = model_root / 'active.json'
    old = json.loads(active.read_text()) if active.exists() else {}
    rows = dataset(root / 'ledger.sqlite3', model_root / 'inputs.sqlite3')
    consumed = set(old.get('cohort_ids', []))
    new_count = len({r['match_id'] for r in rows} - consumed)
    status: dict[str, Any] = {'state': 'waiting', 'checked_at': datetime.now(timezone.utc).isoformat(),
                             'eligible_matches': len({r['match_id'] for r in rows}), 'eligible_decisions': len(rows), 'new_matches': new_count, 'cycle_matches': cycle,
                             'limits': limits, 'minimum_matches': 100, 'error': ''}
    model: dict[str, Any] = {}
    if new_count >= cycle and status['eligible_matches'] >= 100:
        status['state'] = 'training'
        atomic_json(model_root / 'status.json', status)
        model = train(rows)
        model['updated_at'] = datetime.now(timezone.utc).isoformat()
        model['version'] = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        model['limits'] = limits
        atomic_json(model_root / (model['version'] + '.json'), model)
        status.update(state='ready', new_matches=0)
    from store.training_data import prospective_metrics
    current = model or (json.loads(active.read_text()) if active.exists() else {})
    atomic_json(model_root / 'prospective.json', prospective_metrics(root / 'ledger.sqlite3', model_root / 'shadow.sqlite3', current))
    if model:
        atomic_json(active, model)
    atomic_json(model_root / 'status.json', status)
    return status


if __name__ == '__main__':
    root = Path(sys.argv[1])
    model_root = root.parent / 'models'
    model_root.mkdir(parents=True, exist_ok=True)
    with (model_root / 'training.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(0)  # Another process owns training for this data directory.
        try:
            run(root, int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
        except Exception as exc:
            path = model_root / 'status.json'
            status = json.loads(path.read_text()) if path.exists() else {}
            atomic_json(path, {**status, 'state': 'failed', 'error': str(exc),
                              'checked_at': datetime.now(timezone.utc).isoformat()})
            raise
