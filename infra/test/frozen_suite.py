"""Run a frozen source snapshot and retain machine-readable evidence."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import time
import unittest


def main() -> int:
    source = Path("/source")
    frozen = Path("/tmp/frozen")
    shutil.copytree(source, frozen, ignore=shutil.ignore_patterns(
        ".git", ".runtime", "build", ".gradle", "node_modules", "__pycache__", "*.pyc",
    ))
    hashes = {str(p.relative_to(frozen)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(frozen.rglob("*")) if p.is_file()}
    os.chdir(frozen)
    sys.path[:0] = [str(frozen / "services/api"), str(frozen / "services"), str(frozen)]
    logging.disable(logging.CRITICAL)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover("tests"))
    report = {"commit": os.environ.get("SOURCE_COMMIT", "uncommitted"), "tests": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors),
              "skipped": len(result.skipped), "seconds": round(time.monotonic() - started, 3),
              "source_sha256": hashes, "scope": "isolated fixtures; no live providers or money"}
    output = Path("/evidence")
    output.mkdir(exist_ok=True)
    (output / "backend-tests.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return int(not result.wasSuccessful() or bool(result.skipped))


if __name__ == "__main__":
    raise SystemExit(main())
