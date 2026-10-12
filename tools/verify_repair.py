#!/usr/bin/env python3
"""Verify the completed TASK-42 catalog and selected remote member hashes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

from store.archive import S3Archive


def verify(root: Path) -> dict[str, object]:
    state = root / "_archive" / "repairs" / "task42"
    archive = S3Archive(root)
    with sqlite3.connect(state / "progress.sqlite3") as db:
        counts = dict(db.execute("SELECT status,count(*) FROM sources GROUP BY status").fetchall())
    with sqlite3.connect(archive.catalog) as db:
        rows = db.execute(
            "SELECT path,object_key,offset,compressed,size,sha256 "
            "FROM files WHERE path LIKE 'ledger/%' ORDER BY path"
        ).fetchall()
        keys = {row[1] for row in rows}
        pack_sizes = dict(db.execute("SELECT object_key,bytes FROM packs").fetchall())
    if counts != {"evidence": 19, "quarantined_model_evidence": 9, "repaired": 843}:
        raise ValueError(f"unexpected repair counts: {counts}")
    if len(rows) < counts["repaired"] or any(
        len(str(row[5])) != 64 or row[4] <= 0 or row[1] not in pack_sizes for row in rows
    ):
        raise ValueError("invalid catalog row")

    def head(key: str) -> tuple[str, int, int]:
        response = archive.client.head_object(Bucket=archive.bucket, Key=key)
        return key, int(response["ContentLength"]), int(pack_sizes[key])

    with ThreadPoolExecutor(max_workers=8) as pool:
        heads = list(pool.map(head, sorted(keys)))
    if any(actual != expected for _, actual, expected in heads):
        raise ValueError("remote pack length mismatch")

    def read_member(row: tuple) -> tuple[str, int, str]:
        raw = b"".join(archive._range_blocks(row[1], row[2], row[3]))
        data = gzip.decompress(raw)
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != row[4] or digest != row[5]:
            raise ValueError(f"remote member hash mismatch: {row[0]}")
        return row[0], len(data), digest

    samples = [rows[0], rows[len(rows) // 2], rows[-1]]
    checked = [read_member(row) for row in samples]
    for name in ("status.json", "completion.json"):
        relative = f"_archive/repairs/task42/{name}"
        record = next(row for row in rows if row[0] == relative) if any(row[0] == relative for row in rows) else None
        if record is None:
            with sqlite3.connect(archive.catalog) as db:
                record = db.execute(
                    "SELECT path,object_key,offset,compressed,size,sha256 FROM files WHERE path=?",
                    (relative,),
                ).fetchone()
        if record is None:
            raise ValueError(f"missing catalog member: {relative}")
        read_member(record)
    return {
        "progress": counts,
        "catalog_ledger_files": len(rows),
        "unique_revised_packs": len(keys),
        "remote_heads_verified": len(heads),
        "range_members_verified": checked,
        "status_members_verified": True,
    }


if __name__ == "__main__":
    print(json.dumps(verify(Path(__file__).resolve().parents[1] / "output"), ensure_ascii=False))
