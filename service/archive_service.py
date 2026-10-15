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
import time
from typing import Any, Iterator

from store.archive import S3Archive, storage_mode
from store.disk_pressure import disk_pressure
from store.postgres import postgres_enabled
from service.model_training import atomic_json

OLD_REPLAY = re.compile(r"^live-replay\.\d{13,20}\.jsonl$")
SEALED_HOUR = re.compile(r"\.(\d{10})\.jsonl\.gz$")
DATABASE_BACKUP_INTERVAL_S = 24 * 60 * 60
DATABASE_BACKUP_STATE = "_archive/db-backup-state.json"
ARCHIVE_BATCH_FILES = 2000
# Deletion is the disk-pressure relief valve.  A cleanup run must not hold
# verified files until a large upload batch finishes; each file gets its own
# upload/catalog/cleanup boundary.  Non-cleanup migrations retain batching.
ARCHIVE_CLEANUP_BATCH_FILES = 1
ARCHIVE_HEARTBEAT_INTERVAL_S = 5
_STATUS_LOCK = threading.Lock()


def database_backups_enabled() -> bool:
    """Opt in to large active-DB snapshots; replay archives remain canonical."""
    # PostgreSQL is the runtime source of truth.  Copying any stale SQLite
    # files into S3 after cutover would make the dashboard report two stores.
    if postgres_enabled():
        return False
    return os.environ.get("ARCHIVE_DB_BACKUPS", "").strip().lower() in {"1", "true", "yes"}


def _file_signature(path: Path) -> list[int]:
    """Return a cheap signature for an active SQLite database and its WAL."""
    values: list[int] = []
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            stat = candidate.stat()
        except OSError:
            values.extend((0, 0))
        else:
            values.extend((stat.st_size, stat.st_mtime_ns))
    return values


def _read_backup_state(root: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads((root / DATABASE_BACKUP_STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_backup_state(root: Path, state: dict[str, dict[str, Any]]) -> None:
    path = root / DATABASE_BACKUP_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def data_files(root: Path) -> Iterator[Path]:
    # Process the largest, fastest-growing ledger first. A full snapshot pass
    # can take hours and leaves the local disk full while replay journals wait.
    for folder in ("ledger", "snapshots", "_trends", "_live", "models"):
        base = root / folder
        for directory, _, files in os.walk(base):
            for filename in sorted(files, key=lambda item: (0 if item.startswith("live-replay") else 1, item)):
                path = Path(directory) / filename
                if path.suffix in (".json", ".jsonl", ".gz"):
                    yield path
    # Repair outputs and database snapshots are immutable once published. They
    # used to be left outside the migration walk and accumulated locally.
    for folder in ("_archive/backups", "_archive/repairs"):
        base = root / folder
        for directory, _, files in os.walk(base):
            for filename in sorted(files):
                path = Path(directory) / filename
                relative = path.relative_to(root)
                if relative in {
                    Path("_archive/catalog.sqlite3"),
                    Path("_archive/migration.lock"),
                    Path("_archive/progress.json"),
                    Path("_archive/status.json"),
                }:
                    continue
                yield path
    for filename in ("decisions.json", "runtime-settings.json", "storage-settings.json"):
        path = root / filename
        if path.exists():
            yield path


def _cursor_covers(root: Path, path: Path, size: int | None = None) -> bool:
    try:
        cursor = json.loads((root / "models" / "replay-cursor.json").read_text(encoding="utf-8"))
        value = int(cursor.get(path.name, 0))
        return value >= (path.stat().st_size if size is None else size)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def cleanable(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    parts = list(relative.parts)
    if not parts:
        return False
    if parts[0] == "snapshots":
        return path.name != "_index.json" and not path.name.startswith(".tmp_")
    if parts[0] == "ledger" and OLD_REPLAY.match(path.name):
        return True  # Old rotated files are permanently sealed.
    if (
        path.name in ("live-replay.jsonl.gz", "decision-runs.jsonl.gz")
        and parts[0] == "ledger"
    ):
        if path.name.startswith("live-replay"):
            return _cursor_covers(root, path)
        return True
    if parts[0] == "_trends" and len(parts) == 2 and path.suffix == ".jsonl":
        return True  # S3 writers now append to per-hour match segments.
    if parts[0] == "_archive" and len(parts) >= 2:
        return parts[1] in {"backups", "repairs"}
    if parts[0] == "_live" and path.suffix == ".gz" and SEALED_HOUR.search(path.name) is None:
        return True  # Legacy compressed journals are sealed after switching backend.
    hour = SEALED_HOUR.search(path.name)
    if hour and hour.group(1) < datetime.now(timezone.utc).strftime("%Y%m%d%H"):
        if path.name.startswith("live-replay."):
            return _cursor_covers(root, path)
        return True
    if (
        parts[0] == "_trends"
        and len(parts) == 3
        and path.stem.isdigit()
        and len(path.stem) == 10
    ):
        return path.stem < datetime.now(timezone.utc).strftime("%Y%m%d%H")
    return False


def backup_databases(root: Path, *, interval_s: int = DATABASE_BACKUP_INTERVAL_S) -> list[Path]:
    """Create infrequent immutable DB snapshots for S3 audit/recovery.

    Copying every active database on every five-minute cycle wasted I/O and
    left each copy in ``_archive``. A successful snapshot is now reused for a
    day; a failed upload leaves the temporary file in place and is retried.
    """
    if disk_pressure(root).get("pause_optional_writes", True):
        return []
    state = _read_backup_state(root)
    now = time.time()
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
        key = str(relative)
        signature = _file_signature(source)
        previous = state.get(key, {})
        if target.exists():
            backups.append(target)
            continue
        if (previous.get("signature") == signature and
                now - float(previous.get("backed_up_at", 0) or 0) < interval_s):
            continue
        with closing(
            sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as src:
            with closing(sqlite3.connect(target)) as dst:
                src.backup(dst, pages=256, sleep=0.01)
        backups.append(target)
    return backups


def record_database_backups(root: Path, sources: list[Path]) -> None:
    state = _read_backup_state(root)
    now = time.time()
    for target in sources:
        try:
            relative = target.relative_to(root / "_archive" / "backups")
            source = root / relative
            state[str(relative)] = {"signature": _file_signature(source), "backed_up_at": now}
        except (ValueError, OSError):
            continue
    _write_backup_state(root, state)


def _batches(paths: Iterator[Path], size: int = ARCHIVE_BATCH_FILES) -> Iterator[list[Path]]:
    batch: list[Path] = []
    for path in paths:
        batch.append(path)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _cleanup_paths(root: Path, archive: S3Archive, paths: Iterator[Path]) -> dict[str, int]:
    """Clean ordinary files and coordinate replay deletion with model reads."""
    ordinary: list[Path] = []
    replay: list[Path] = []
    replay_meta: dict[str, Any] = {}
    replay_gaps: dict[str, Any] = {}
    remote_replay_ready = False
    try:
        loaded = json.loads((root / "models" / "replay-source-meta.json").read_text())
        replay_meta = loaded.get("sources", {}) if isinstance(loaded, dict) else {}
    except (OSError, ValueError, TypeError):
        replay_meta = {}
    try:
        loaded = json.loads((root / "models" / "replay-gaps.json").read_text())
        replay_gaps = loaded.get("gaps", {}) if isinstance(loaded, dict) else {}
    except (OSError, ValueError, TypeError):
        replay_gaps = {}
    try:
        dashboard = json.loads((root / "models" / "dashboard.json").read_text())
        replay_status = dashboard.get("replay", {}) if isinstance(dashboard, dict) else {}
        remote_replay_ready = (
            replay_status.get("source_protocol") == 1
            and replay_status.get("source_backend") == "s3"
            and replay_status.get("remote_read_verified") is True
        )
    except (OSError, ValueError, TypeError):
        pass
    for path in paths:
        if not path.is_file():
            continue
        replay_path = path.name.startswith("live-replay")
        sealed_replay = bool(
            path.name.startswith("live-replay.")
            and (hour := SEALED_HOUR.search(path.name))
            and hour.group(1) < datetime.now(timezone.utc).strftime("%Y%m%d%H")
        )
        if not cleanable(root, path) and not (replay_path and remote_replay_ready and sealed_replay):
            continue
        if replay_path:
            try:
                cursor = json.loads((root / "models" / "replay-cursor.json").read_text())
                offset = int(cursor.get(path.name, 0))
                proof = replay_meta.get(path.name)
                with closing(archive._db()) as db:
                    catalog = db.execute(
                        'SELECT size,mtime_ns,sha256,object_key FROM files WHERE path=?',
                        (path.relative_to(root).as_posix(),)).fetchone()
                if (path.name in replay_gaps or catalog is None
                        or int(catalog[0]) != path.stat().st_size
                        or int(catalog[1]) != path.stat().st_mtime_ns
                        or (not remote_replay_ready and (
                            not isinstance(proof, dict)
                            or offset < path.stat().st_size
                            or int(proof.get("size", -1)) != path.stat().st_size
                            or int(proof.get("mtime_ns", -1)) != path.stat().st_mtime_ns
                            or (proof.get("remote") is True
                                and proof.get("object_key") != catalog[3])
                            or (proof.get("remote") is True
                                and proof.get("sha256") != catalog[2])))):
                    continue
            except (OSError, ValueError, TypeError):
                continue
        (replay if path.name.startswith("live-replay") else ordinary).append(path)
    totals = {"files": 0, "bytes": 0}
    if ordinary:
        cleaned = archive.cleanup(ordinary)
        totals["files"] += cleaned["files"]
        totals["bytes"] += cleaned["bytes"]
    # A replay cleanup can perform a remote pack verification and a full local
    # SHA256 read. Hold the writer lock for one file only so a large catalog
    # cleanup cannot starve the model worker's bounded shared-lock replay.
    lock_path = root / "models" / "replay.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for path in replay:
        try:
            token = archive.prepare_cleanup(path)
        except (OSError, ValueError, TypeError):
            continue
        if token is None:
            continue
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not _replay_cleanup_allowed(root, archive, path):
                fcntl.flock(lock, fcntl.LOCK_UN)
                continue
            cleaned = archive.commit_cleanup(path, token)
            fcntl.flock(lock, fcntl.LOCK_UN)
        totals["files"] += cleaned["files"]
        totals["bytes"] += cleaned["bytes"]
    return totals


def _replay_cleanup_allowed(root: Path, archive: S3Archive, path: Path) -> bool:
    """Re-read replay gates immediately before the unlink commit."""
    replay_meta: dict[str, Any] = {}
    replay_gaps: dict[str, Any] = {}
    remote_replay_ready = False
    try:
        loaded = json.loads((root / "models" / "replay-source-meta.json").read_text())
        replay_meta = loaded.get("sources", {}) if isinstance(loaded, dict) else {}
    except (OSError, ValueError, TypeError):
        pass
    try:
        loaded = json.loads((root / "models" / "replay-gaps.json").read_text())
        replay_gaps = loaded.get("gaps", {}) if isinstance(loaded, dict) else {}
    except (OSError, ValueError, TypeError):
        pass
    try:
        loaded = json.loads((root / "models" / "dashboard.json").read_text())
        replay_status = loaded.get("replay", {}) if isinstance(loaded, dict) else {}
        remote_replay_ready = (
            replay_status.get("source_protocol") == 1
            and replay_status.get("source_backend") == "s3"
            and replay_status.get("remote_read_verified") is True
        )
    except (OSError, ValueError, TypeError):
        # A local source proof is sufficient when the optional dashboard has
        # not been published yet; remote-read readiness remains false.
        remote_replay_ready = False
    try:
        if path.name in replay_gaps or not path.is_file():
            return False
        sealed = bool(
            (hour := SEALED_HOUR.search(path.name))
            and hour.group(1) < datetime.now(timezone.utc).strftime("%Y%m%d%H")
        )
        if not cleanable(root, path) and not (remote_replay_ready and sealed):
            return False
        cursor = json.loads((root / "models" / "replay-cursor.json").read_text())
        offset = int(cursor.get(path.name, 0))
        stat = path.stat()
        with closing(archive._db()) as db:
            catalog = db.execute(
                'SELECT size,mtime_ns,sha256,object_key FROM files WHERE path=?',
                (path.relative_to(root).as_posix(),),
            ).fetchone()
        if catalog is None or int(catalog[0]) != stat.st_size or int(catalog[1]) != stat.st_mtime_ns:
            return False
        if remote_replay_ready:
            return True
        proof = replay_meta.get(path.name)
        return (
            isinstance(proof, dict)
            and offset >= stat.st_size
            and int(proof.get("size", -1)) == stat.st_size
            and int(proof.get("mtime_ns", -1)) == stat.st_mtime_ns
            and (proof.get("remote") is not True or proof.get("object_key") == catalog[3])
            and (proof.get("remote") is not True or proof.get("sha256") == catalog[2])
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _cleanup_catalogued(root: Path, archive: S3Archive) -> dict[str, int]:
    """Release already-uploaded local files before scanning new input."""
    with closing(archive._db()) as db:
        paths = [root / row[0] for row in db.execute(
            "SELECT path FROM files WHERE cleaned=0 ORDER BY path"
        )]
    totals = {"files": 0, "bytes": 0}
    for batch in _batches(iter(paths), size=ARCHIVE_CLEANUP_BATCH_FILES):
        cleaned = _cleanup_paths(root, archive, iter(batch))
        totals["files"] += cleaned["files"]
        totals["bytes"] += cleaned["bytes"]
        if cleaned['files']:
            _status_update(root / '_archive' / 'status.json', phase='cleanup',
                           cleanup_released_files=totals['files'], cleanup_released_bytes=totals['bytes'],
                           last_cleaned_path=str(batch[-1].relative_to(root)))
    return totals


def _status_update_locked(path: Path, **updates: Any) -> None:
    """Publish a small durable heartbeat while a long archive pass runs."""
    current: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            current = loaded
    except (OSError, ValueError, TypeError):
        pass
    current.update(updates)
    current["at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(path, current)


def _status_update(path: Path, **updates: Any) -> None:
    with _STATUS_LOCK:
        _status_update_locked(path, **updates)


def _migrate_resilient(root: Path, archive: S3Archive, batch: list[Path], *, cleanup: bool,
                       status_path: Path, retry_count: int) -> tuple[dict[str, int], int]:
    """Keep one bad/rate-limited file from terminating the archive worker.

    A pack is retried as a whole first. If that fails, files are isolated so a
    transiently missing or permanently malformed file does not block all other
    local data from being released. The failed file remains local and is retried
    on the next worker cycle.
    """
    zero = {"uploaded_files": 0, "uploaded_bytes": 0,
            "cleaned_files": 0, "cleaned_bytes": 0}
    if not batch:
        return zero, retry_count
    _status_update(status_path, phase="uploading", last_path=str(batch[0].relative_to(root)),
                   last_error="")
    try:
        return archive.migrate(batch, cleanup=False), retry_count
    except FileNotFoundError:
        # The collector rotated or removed the file between walk and stat.
        return zero, retry_count
    except Exception as exc:
        retry_count += 1
        _status_update(status_path, phase="retrying", retry_count=retry_count,
                       last_error=type(exc).__name__, last_path=str(batch[0].relative_to(root)))
        if len(batch) > 1:
            totals = dict(zero)
            for path in batch:
                current, retry_count = _migrate_resilient(
                    root, archive, [path], cleanup=cleanup, status_path=status_path,
                    retry_count=retry_count)
                for key in totals:
                    totals[key] += current[key]
            return totals, retry_count
        return zero, retry_count


def run(root: Path, *, cleanup: bool, once: bool) -> None:
    event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: event.set())
    signal.signal(signal.SIGINT, lambda *_: event.set())
    status_path = root / "_archive" / "status.json"
    # Replace a previous crash marker immediately. A multi-million-file S3
    # pass can take time before the first pack completes, and a stale
    # ``running=false`` status made an active worker look permanently failed.
    atomic_json(status_path, {
        "running": True,
        "error": "",
        "phase": "waiting_for_s3",
        "retry_count": 0,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    while storage_mode(root) != "s3" and not event.is_set():
        if once:
            raise ValueError("迁移前需要启用 S3 存储配置")
        event.wait(30)
    if event.is_set():
        return
    archive = S3Archive(root)
    retry_count = 0
    def heartbeat() -> None:
        while not event.wait(ARCHIVE_HEARTBEAT_INTERVAL_S):
            _status_update(status_path, running=True, disk_pressure=disk_pressure(root))
    heartbeat_thread = threading.Thread(target=heartbeat, name="archive-heartbeat", daemon=True)
    heartbeat_thread.start()
    with (archive.state / "migration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while not event.is_set():
            # Cleanup only becomes possible when the deployed reader supports S3.
            # Explicit --cleanup is still required; upload failures never delete.
            mode = storage_mode(root)
            if cleanup and mode != "s3":
                raise ValueError("清理前必须启用 S3 回读后端")
            # Create snapshots before walking the tree. Upload and clean in
            # bounded batches so a multi-million-file catalog cannot postpone
            # all cleanup until the end of a many-hour pass.
            backups = backup_databases(root) if database_backups_enabled() else []
            uploaded = {"uploaded_files": 0, "uploaded_bytes": 0,
                        "cleaned_files": 0, "cleaned_bytes": 0}
            cleaned = {"files": 0, "bytes": 0}
            if cleanup:
                # Do this before the filesystem walk. A previous run may have
                # uploaded millions of files but stopped before its final
                # cleanup phase; waiting for another full walk wastes disk.
                try:
                    _status_update(status_path, phase="cleanup", retry_count=retry_count)
                    cleaned = _cleanup_catalogued(root, archive)
                except FileNotFoundError:
                    cleaned = {"files": 0, "bytes": 0}
                except Exception as exc:
                    retry_count += 1
                    _status_update(status_path, phase="retrying", retry_count=retry_count,
                                   last_error=type(exc).__name__)
            batch_size = ARCHIVE_CLEANUP_BATCH_FILES if cleanup else ARCHIVE_BATCH_FILES
            for batch in _batches(data_files(root), size=batch_size):
                current, retry_count = _migrate_resilient(
                    root, archive, batch, cleanup=cleanup, status_path=status_path,
                    retry_count=retry_count)
                for key in uploaded:
                    uploaded[key] += current.get(key, 0)
                if cleanup:
                    try:
                        _status_update(status_path, phase="cleanup", retry_count=retry_count)
                        current_cleaned = _cleanup_paths(root, archive, iter(batch))
                        cleaned["files"] += current_cleaned["files"]
                        cleaned["bytes"] += current_cleaned["bytes"]
                    except FileNotFoundError:
                        pass
                    except Exception as exc:
                        retry_count += 1
                        _status_update(status_path, phase="retrying", retry_count=retry_count,
                                       last_error=type(exc).__name__)
            if backups:
                backup_upload, retry_count = _migrate_resilient(
                    root, archive, backups, cleanup=cleanup, status_path=status_path,
                    retry_count=retry_count)
                for key in uploaded:
                    uploaded[key] += backup_upload.get(key, 0)
                if cleanup:
                    record_database_backups(root, backups)
            prior_status: dict[str, Any] = {}
            try:
                loaded = json.loads(status_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    prior_status = loaded
            except (OSError, ValueError, TypeError):
                pass
            status = {
                **archive.stats(),
                "at": datetime.now(timezone.utc).isoformat(),
                "last_upload": uploaded,
                "last_cleanup": cleaned,
                "running": not once,
                "error": "" if retry_count == 0 else "部分文件待重试",
                "last_error": prior_status.get("last_error", ""),
                "phase": "idle" if retry_count == 0 else "retrying",
                "retry_count": retry_count,
            }
            _status_update(status_path, **status)
            print(json.dumps(status, ensure_ascii=False), flush=True)
            if once:
                break
            event.wait(300)
    event.set()
    heartbeat_thread.join(timeout=1)


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
