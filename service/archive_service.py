"""S3 migration/archival worker. Upload, verify, catalogue, then clean sealed files."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import threading
from typing import Iterator

from store.archive import S3Archive, storage_mode
from service.model_training import atomic_json

OLD_REPLAY = re.compile(r"^live-replay\.\d{13,20}\.jsonl$")
SEALED_HOUR = re.compile(r"\.(\d{10})\.jsonl\.gz$")


def data_files(root: Path) -> Iterator[Path]:
    for folder in ("snapshots", "ledger", "_trends", "_live", "models"):
        base = root / folder
        for directory, _, files in os.walk(base):
            for filename in sorted(files):
                path = Path(directory) / filename
                if path.suffix in (".json", ".jsonl", ".gz"):
                    yield path
    for filename in ("decisions.json", "runtime-settings.json", "storage-settings.json"):
        path = root / filename
        if path.exists():
            yield path


def cleanable(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    if relative.parts[0] == "snapshots":
        return path.name != "_index.json" and not path.name.startswith(".tmp_")
    if relative.parts[0] == "ledger" and OLD_REPLAY.match(path.name):
        return True  # Old rotated files are permanently sealed.
    if (
        path.name in ("live-replay.jsonl.gz", "decision-runs.jsonl.gz")
        and relative.parts[0] == "ledger"
    ):
        if path.name.startswith("live-replay"):
            cursor = root / "models" / "replay-cursor.json"
            return (
                cursor.exists()
                and json.loads(cursor.read_text()).get(path.name, 0) >= path.stat().st_size
            )
        return True
    if relative.parts[0] == "_trends" and len(relative.parts) == 2 and path.suffix == ".jsonl":
        return True  # S3 writers now append to per-hour match segments.
    if relative.parts[0] == "_live" and path.suffix == ".gz" and not SEALED_HOUR.search(path.name):
        return True  # Legacy compressed journals are sealed after switching backend.
    hour = SEALED_HOUR.search(path.name)
    if hour and hour[1] < datetime.now(timezone.utc).strftime("%Y%m%d%H"):
        if path.name.startswith("live-replay."):
            cursor = root / "models" / "replay-cursor.json"
            return (
                cursor.exists()
                and json.loads(cursor.read_text()).get(path.name, 0) >= path.stat().st_size
            )
        return True
    if (
        relative.parts[0] == "_trends"
        and len(relative.parts) == 3
        and path.stem.isdigit()
        and len(path.stem) == 10
    ):
        return path.stem < datetime.now(timezone.utc).strftime("%Y%m%d%H")
    return False


def backup_databases(root: Path) -> list[Path]:
    backups = []
    for relative in (
        "ledger/ledger.sqlite3",
        "betting-orders.sqlite3",
        "models/inputs.sqlite3",
        "models/shadow.sqlite3",
    ):
        source = root / relative
        if not source.exists():
            continue
        target = root / "_archive" / "backups" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(
            sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as src:
            with closing(sqlite3.connect(target)) as dst:
                src.backup(dst, pages=256, sleep=0.01)
        backups.append(target)
    return backups


def run(root: Path, *, cleanup: bool, once: bool) -> None:
    event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: event.set())
    signal.signal(signal.SIGINT, lambda *_: event.set())
    while storage_mode(root) != "s3" and not event.is_set():
        if once:
            raise ValueError("迁移前需要启用 S3 存储配置")
        event.wait(30)
    if event.is_set():
        return
    archive = S3Archive(root)
    with (archive.state / "migration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while not event.is_set():
            # Cleanup only becomes possible when the deployed reader supports S3.
            # Explicit --cleanup is still required; upload failures never delete.
            mode = storage_mode(root)
            if cleanup and mode != "s3":
                raise ValueError("清理前必须启用 S3 回读后端")
            uploaded = archive.migrate(data_files(root), cleanup=False)
            archive.migrate(backup_databases(root), cleanup=False)
            cleaned = {"files": 0, "bytes": 0}
            if cleanup:
                cleaned = archive.cleanup(p for p in data_files(root) if cleanable(root, p))
            status = {
                **archive.stats(),
                "at": datetime.now(timezone.utc).isoformat(),
                "last_upload": uploaded,
                "last_cleanup": cleaned,
                "running": not once,
                "error": "",
            }
            atomic_json(root / "_archive" / "status.json", status)
            print(json.dumps(status, ensure_ascii=False), flush=True)
            if once:
                break
            event.wait(300)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/app/output"))
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--rebuild", action="store_true", help="从S3清单恢复本地索引")
    parser.add_argument("--restore", action="store_true", help="回填缺失文件，完成后可切回local")
    args = parser.parse_args()
    try:
        if args.rebuild or args.restore:
            archive = S3Archive(args.root)
            if args.rebuild:
                print(json.dumps({"catalogued_files": archive.rebuild_catalog()}), flush=True)
            if args.restore:
                print(json.dumps({"restored_files": archive.restore()}), flush=True)
            return
        run(args.root.resolve(), cleanup=args.cleanup, once=args.once)
    except Exception as exc:
        # SDK exception payloads may contain gateway internals; keep the status
        # diagnostic small and credential-free.
        atomic_json(
            args.root / "_archive" / "status.json",
            {
                "running": False,
                "error": type(exc).__name__,
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )
        raise


if __name__ == "__main__":
    main()
