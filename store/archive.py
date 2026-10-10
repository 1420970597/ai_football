"""Local archives or verified S3 packs, preserving exact paths and file hashes."""

from __future__ import annotations

from contextlib import closing
import gzip
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any, Iterable, Sequence, List
import uuid
from functools import lru_cache

PACK_BYTES = 32 * 1024**2
BATCH_FILES = 2000


def storage_mode(root: Path) -> str:
    mode = os.environ.get("ARCHIVE_BACKEND", "")
    path = root / "storage-settings.json"
    if not mode and path.exists():
        mode = json.loads(path.read_text()).get("backend", "local")
    if mode not in ("", "local", "s3"):
        raise ValueError("ARCHIVE_BACKEND 必须是 local 或 s3")
    return mode or "local"


def output_root(path: Path) -> Path | None:
    resolved = path.resolve()
    for parent in (resolved, *resolved.parents):
        if parent.name == "output" or (parent / "storage-settings.json").exists():
            return parent
    return None


class S3Archive:
    def __init__(self, root: Path, client: Any = None, *, config_path: Path | None = None) -> None:
        self.root = root.resolve()
        self.state = self.root / "_archive"
        self.state.mkdir(parents=True, exist_ok=True)
        self.catalog = self.state / "catalog.sqlite3"
        self._transfer_config: Any = None
        self._verified: set[str] = set()
        if client is None:
            config_path = config_path or Path(
                os.environ.get(
                    "S3_CONFIG_FILE", str(self.root.parent / "client" / "credentials.json")
                )
            )
            cfg = json.loads(config_path.read_text())
            ca = (config_path.parent / cfg["ca_file"]).resolve()
            if not ca.is_file():
                raise ValueError("S3 CA 文件不存在")
            sdk = importlib.import_module("boto3")
            botoconfig = importlib.import_module("botocore.config")
            client = sdk.client(
                "s3",
                endpoint_url=cfg["endpoint"],
                region_name=cfg.get("region", "us-east-1"),
                aws_access_key_id=cfg["access_key"],
                aws_secret_access_key=cfg["secret_key"],
                verify=str(ca),
                config=botoconfig.Config(
                    signature_version="s3v4",
                    s3={"addressing_style": cfg.get("addressing_style", "path")},
                    connect_timeout=10,
                    read_timeout=120,
                    retries={"max_attempts": 3},
                ),
            )
            self.bucket = cfg["bucket"]
            self._transfer_config = importlib.import_module("boto3.s3.transfer").TransferConfig(
                max_concurrency=2, use_threads=True
            )
        else:
            self.bucket = "test-archive"
        self.client = client
        with closing(self._db()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, object_key TEXT NOT NULL, "
                "offset INTEGER NOT NULL, compressed INTEGER NOT NULL, size INTEGER NOT NULL, "
                "sha256 TEXT NOT NULL, mtime_ns INTEGER NOT NULL, cleaned INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS packs (object_key TEXT PRIMARY KEY, bytes INTEGER, sha256 TEXT, at REAL)"
            )

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.catalog, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def list(self, directory: Path) -> list[Path]:
        prefix = self._relative(directory).rstrip("/") + "/"
        with closing(self._db()) as db:
            return [
                self.root / r[0]
                for r in db.execute(
                    "SELECT path FROM files WHERE path>=? AND path<? ORDER BY path",
                    (prefix, prefix + "\uffff"),
                )
            ]

    def contains(self, path: Path) -> bool:
        with closing(self._db()) as db:
            return (
                db.execute("SELECT 1 FROM files WHERE path=?", (self._relative(path),)).fetchone()
                is not None
            )

    def read_many(self, paths: Iterable[Path]) -> dict[Path, bytes]:
        result = {}
        remote: dict[str, list[tuple[Path, int, int, int, str]]] = {}
        with closing(self._db()) as db:
            for path in paths:
                if path.exists():
                    try:
                        result[path] = path.read_bytes()
                        continue
                    except FileNotFoundError:
                        pass  # Cleanup after the existence check; read the committed object.
                row = db.execute(
                    "SELECT object_key,offset,compressed,size,sha256 FROM files WHERE path=?",
                    (self._relative(path),),
                ).fetchone()
                if row is None:
                    raise FileNotFoundError(str(path))
                remote.setdefault(row[0], []).append((path, *row[1:]))
        for key, members in remote.items():
            first = min(m[1] for m in members)
            end = max(m[1] + m[2] for m in members)
            response = self.client.get_object(
                Bucket=self.bucket, Key=key, Range=f"bytes={first}-{end - 1}"
            )
            body = response["Body"]
            try:
                payload = body.read()
            finally:
                body.close()
            if len(payload) != end - first:
                raise OSError("S3 返回不完整的归档范围")
            for path, offset, length, size, digest in members:
                raw = gzip.decompress(payload[offset - first : offset - first + length])
                if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
                    raise OSError("S3 归档内容校验失败：" + path.name)
                result[path] = raw
        return result

    def _upload_verified(self, key: str, stream: Any, size: int, digest: str) -> None:
        stream.seek(0)
        self.client.upload_fileobj(
            stream,
            self.bucket,
            key,
            ExtraArgs={"Metadata": {"sha256": digest}},
            Config=self._transfer_config,
        )
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        actual = hashlib.sha256()
        total = 0
        body = response["Body"]
        try:
            while True:
                block = body.read(1024**2)
                if not block:
                    break
                actual.update(block)
                total += len(block)
        finally:
            body.close()
        if total != size or actual.hexdigest() != digest:
            raise OSError("S3 上传后回读 SHA256 校验失败")

    def migrate(self, paths: Iterable[Path], *, cleanup: bool = False) -> dict[str, int]:
        totals = {"uploaded_files": 0, "uploaded_bytes": 0, "cleaned_files": 0, "cleaned_bytes": 0}
        batch: list[Path] = []
        size = 0
        with closing(self._db()) as db:
            for path in paths:
                if not path.is_file() or path.is_symlink():
                    continue
                stat = path.stat()
                old = db.execute(
                    "SELECT size,mtime_ns FROM files WHERE path=?", (self._relative(path),)
                ).fetchone()
                if old and tuple(old) == (stat.st_size, stat.st_mtime_ns):
                    if cleanup:
                        n = self.cleanup([path])
                        totals["cleaned_files"] += n["files"]
                        totals["cleaned_bytes"] += n["bytes"]
                    continue
                batch.append(path)
                size += stat.st_size
                if len(batch) >= BATCH_FILES or size >= PACK_BYTES:
                    self._pack(batch, cleanup, totals)
                    batch = []
                    size = 0
        if batch:
            self._pack(batch, cleanup, totals)
        return totals

    def _pack(self, paths: Sequence[Path], cleanup: bool, totals: dict[str, int]) -> None:
        records: List[dict[str, Any]] = []
        key = "ai-football/packs/" + uuid.uuid4().hex + ".pack"
        # Temporary pack is removed only after remote verification. Original
        # source files are untouched on all upload/checksum failures.
        with tempfile.TemporaryFile(dir=self.state) as stream:
            for path in paths:
                before = path.stat()
                offset = stream.tell()
                file_hash = hashlib.sha256()
                count = 0
                with (
                    path.open("rb") as original,
                    gzip.GzipFile(
                        fileobj=stream, mode="wb", compresslevel=1, mtime=0
                    ) as compressed,
                ):
                    while True:
                        chunk = original.read(1024**2)
                        if not chunk:
                            break
                        count += len(chunk)
                        file_hash.update(chunk)
                        compressed.write(chunk)
                after = path.stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    stream.seek(offset)
                    stream.truncate()
                    continue
                records.append(
                    {
                        "path": self._relative(path),
                        "object_key": key,
                        "offset": offset,
                        "compressed": stream.tell() - offset,
                        "size": count,
                        "sha256": file_hash.hexdigest(),
                        "mtime_ns": after.st_mtime_ns,
                    }
                )
            if not records:
                return
            length = stream.tell()
            stream.seek(0)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            self._upload_verified(key, stream, length, digest)
            self._verified.add(key)
            manifest = gzip.compress(
                json.dumps(
                    {"version": 1, "key": key, "bytes": length, "sha256": digest, "files": records},
                    ensure_ascii=False,
                ).encode(),
                compresslevel=1,
                mtime=0,
            )
            self._upload_verified(
                key + ".manifest.json.gz",
                io.BytesIO(manifest),
                len(manifest),
                hashlib.sha256(manifest).hexdigest(),
            )
            with closing(self._db()) as db, db:
                db.executemany(
                    "INSERT OR REPLACE INTO files(path,object_key,offset,compressed,size,sha256,mtime_ns,cleaned) "
                    "VALUES (:path,:object_key,:offset,:compressed,:size,:sha256,:mtime_ns,0)",
                    records,
                )
                db.execute(
                    "INSERT OR REPLACE INTO packs VALUES (?,?,?,?)",
                    (key, length, digest, time.time()),
                )
            totals["uploaded_files"] += len(records)
            totals["uploaded_bytes"] += sum(r["size"] for r in records)
        if cleanup:
            cleaned = self.cleanup([self.root / r["path"] for r in records])
            totals["cleaned_files"] += cleaned["files"]
            totals["cleaned_bytes"] += cleaned["bytes"]
        progress = self.state / "progress.json"
        temporary = progress.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({**totals, "at": time.time(), "backend": "s3", "phase": "uploading"})
        )
        os.replace(temporary, progress)

    def cleanup(self, paths: Iterable[Path]) -> dict[str, int]:
        """Only immutable/closed archives; caller must exclude active state."""
        total = {"files": 0, "bytes": 0}
        with closing(self._db()) as db, db:
            for path in paths:
                row = db.execute(
                    "SELECT size,mtime_ns,sha256,object_key FROM files WHERE path=?",
                    (self._relative(path),),
                ).fetchone()
                if row is None or not path.exists():
                    continue
                stat = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != (row[0], row[1]):
                    continue
                if row[3] not in self._verified:
                    self.verify_pack(row[3])
                    self._verified.add(row[3])
                with path.open("rb") as original:
                    if hashlib.file_digest(original, "sha256").hexdigest() != row[2]:
                        continue
                path.unlink()
                db.execute("UPDATE files SET cleaned=1 WHERE path=?", (self._relative(path),))
                total["files"] += 1
                total["bytes"] += stat.st_size
        return total

    def verify_pack(self, key: str) -> None:
        with closing(self._db()) as db:
            expected = db.execute(
                "SELECT bytes,sha256 FROM packs WHERE object_key=?", (key,)
            ).fetchone()
        if expected is None:
            raise OSError("归档缺少完整对象校验信息")
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        digest, size = hashlib.sha256(), 0
        body = response["Body"]
        try:
            while block := body.read(1024**2):
                digest.update(block)
                size += len(block)
        finally:
            body.close()
        if (size, digest.hexdigest()) != tuple(expected):
            raise OSError("清理前远端归档校验失败")

    def rebuild_catalog(self) -> int:
        """Recover the local index from permanent remote pack manifests."""
        total = 0
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix="ai-football/packs/"
        )
        for page in pages:
            for item in page.get("Contents", []):
                key = item["Key"]
                if not key.endswith(".manifest.json.gz"):
                    continue
                response = self.client.get_object(Bucket=self.bucket, Key=key)
                body = response["Body"]
                try:
                    raw = body.read()
                finally:
                    body.close()
                if hashlib.sha256(raw).hexdigest() != response.get("Metadata", {}).get("sha256"):
                    raise OSError("归档清单 SHA256 校验失败")
                manifest = json.loads(gzip.decompress(raw))
                if manifest["version"] != 1 or manifest["key"] != key[:-17]:
                    raise ValueError("归档清单版本或对象不匹配")
                records = manifest["files"]
                for record in records:
                    relative = Path(record["path"])
                    if (
                        relative.is_absolute()
                        or ".." in relative.parts
                        or record["object_key"] != manifest["key"]
                    ):
                        raise ValueError("无效归档路径")
                with closing(self._db()) as db, db:
                    db.execute(
                        "INSERT OR REPLACE INTO packs VALUES (?,?,?,?)",
                        (manifest["key"], manifest["bytes"], manifest["sha256"], time.time()),
                    )
                    db.executemany(
                        "INSERT INTO files(path,object_key,offset,compressed,size,sha256,mtime_ns) "
                        "VALUES (:path,:object_key,:offset,:compressed,:size,:sha256,:mtime_ns) "
                        "ON CONFLICT(path) DO UPDATE SET object_key=excluded.object_key,offset=excluded.offset,"
                        "compressed=excluded.compressed,size=excluded.size,sha256=excluded.sha256,mtime_ns=excluded.mtime_ns "
                        "WHERE excluded.mtime_ns>=files.mtime_ns",
                        records,
                    )
                total += len(records)
        return total

    def restore(self, paths: Iterable[Path] | None = None) -> int:
        """Hydrate missing files before switching back to local storage."""
        restored = 0
        if paths is None:
            with closing(self._db()) as db:
                paths = [
                    self.root / row[0] for row in db.execute("SELECT path FROM files ORDER BY path")
                ]
        for path in paths:
            if path.exists():
                continue
            raw = self.read_many([path])[path]
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
                temporary = Path(target.name)
            try:
                # Do not overwrite operational state created during restoration.
                os.link(temporary, path)
            except FileExistsError:
                continue
            finally:
                temporary.unlink()
            with closing(self._db()) as db, db:
                row = db.execute(
                    "SELECT mtime_ns FROM files WHERE path=?", (self._relative(path),)
                ).fetchone()
                os.utime(path, ns=(row[0], row[0]))
                db.execute("UPDATE files SET cleaned=0 WHERE path=?", (self._relative(path),))
            restored += 1
        return restored

    def stats(self) -> dict[str, Any]:
        with closing(self._db()) as db:
            files, size, cleaned = db.execute(
                "SELECT count(*),coalesce(sum(size),0),coalesce(sum(cleaned),0) FROM files"
            ).fetchone()
            objects, stored = db.execute(
                "SELECT count(*),coalesce(sum(bytes),0) FROM packs"
            ).fetchone()
        return {
            "backend": "s3",
            "bucket": self.bucket,
            "files": files,
            "bytes": size,
            "packs": objects,
            "object_bytes": stored,
            "cleaned_files": cleaned,
            "verification": "远端回读 SHA256 与长度核验",
        }


def archive_for(path: Path) -> S3Archive | None:
    root = output_root(path)
    return _archive(root) if root is not None and storage_mode(root) == "s3" else None


@lru_cache(maxsize=4)
def _archive(root: Path) -> S3Archive:
    return S3Archive(root)


def journal_path(path: Path) -> Path:
    """S3 mode seals hourly local chunks; all remote chunks remain permanent."""
    root = output_root(path)
    if root is None or storage_mode(root) != "s3":
        return path
    from datetime import datetime, timezone

    hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    if path.name.endswith(".jsonl.gz"):
        return path.with_name(path.name[:-9] + "." + hour + ".jsonl.gz")
    return path
