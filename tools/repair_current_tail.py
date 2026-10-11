#!/usr/bin/env python3
"""Repair sealed files written after the frozen S3 catalog cut-off.

The archive repair worker intentionally uses a frozen catalog. Callers must
pass only rotated files; active producer files remain untouched.
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.history_repair import repair_grade, repair_replay
from service.model_training import atomic_json
from store.archive import S3Archive
from store.training_data import snapshot_inputs, write_inputs


def _opener(path: Path) -> Callable[..., Any]:
    return gzip.open if path.name.endswith(".gz") else open


def repair_file(root: Path, path: Path, finals: Mapping[str, Any], archive: S3Archive) -> dict[str, Any]:
    """Rewrite one stopped active file and publish it under its original path."""
    if not path.is_file():
        raise FileNotFoundError(path)
    state = root / "_archive" / "repairs" / "task42" / "tails"
    state.mkdir(parents=True, exist_ok=True)
    suffix = ".jsonl.gz" if path.name.endswith(".gz") else ".jsonl"
    staged = state / (path.name + "." + hashlib.sha256(str(path).encode()).hexdigest()[:12] + ".tmp" + suffix)
    replay = path.name.startswith("live-replay")
    decision = path.name.startswith("ledger")
    opener = _opener(path)
    count = 0
    rebuilt_inputs = 0
    states: dict[str, int] = {}
    inputs: list[dict[str, Any]] = []
    writer = gzip.open(staged, "wb", compresslevel=1) if path.name.endswith(".gz") else staged.open("wb")
    with opener(path, "rb") as source, writer as target:
        for line_no, line in enumerate(source, 1):
            row = json.loads(line)
            if replay:
                revised = repair_replay(row, finals)
                batch = snapshot_inputs(revised)
                inputs.extend(batch)
                rebuilt_inputs += len(batch)
                if len(inputs) >= 1000:
                    write_inputs(root / 'models' / 'inputs.sqlite3', inputs, repair=True)
                    inputs.clear()
            elif decision:
                revised = repair_grade(row, finals)
            else:
                # Review journals are evidence, not decision records.  Keep
                # their payload byte-for-byte equivalent and mark coverage.
                revised = dict(row)
                revised.setdefault("repair_status", "preserved_non_decision_archive")
            revised["repair_source"] = {"path": str(path.relative_to(root)), "record": line_no}
            status = str(revised.get("repair_status", "preserved"))
            states[status] = states.get(status, 0) + 1
            target.write(json.dumps(revised, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
            count += 1
    if inputs:
        write_inputs(root / 'models' / 'inputs.sqlite3', inputs, repair=True)
    stamp = max(time.time_ns(), path.stat().st_mtime_ns + 1)
    os.utime(staged, ns=(stamp, stamp))
    archive.revise(path, staged)
    if replay:
        lock_path = root / 'models' / 'replay.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                os.replace(staged, path)
                os.utime(path, ns=(stamp, stamp))
                cursor_path = root / 'models' / 'replay-cursor.json'
                cursor = json.loads(cursor_path.read_text()) if cursor_path.exists() else {}
                cursor[path.name] = path.stat().st_size
                atomic_json(cursor_path, cursor)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    else:
        os.replace(staged, path)
        os.utime(path, ns=(stamp, stamp))
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path.relative_to(root)), "records": count, "states": states,
            "rebuilt_inputs": rebuilt_inputs,
            "source_sha256": digest}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--finals", type=Path, required=True)
    parser.add_argument("paths", type=Path, nargs="+")
    args = parser.parse_args()
    root = args.root.resolve()
    archive = S3Archive(root)
    finals = json.loads(args.finals.read_text())
    results = [repair_file(root, (p if p.is_absolute() else root / p), finals, archive)
               for p in args.paths]
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
