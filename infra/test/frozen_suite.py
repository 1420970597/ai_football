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
        ".env", ".env.*", "*.jks", "*.keystore",
    ))
    hashes = {str(p.relative_to(frozen)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(frozen.rglob("*")) if p.is_file()}
    os.chdir(frozen)
    sys.path[:0] = [str(frozen / "services/api"), str(frozen / "services"), str(frozen)]
    # Silence application handlers without disabling per-test log capture.
    # Global disable() also suppresses unittest.assertLogs' own handler.
    logging.disable(logging.NOTSET)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(logging.NullHandler())
    root_logger.setLevel(logging.CRITICAL)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover("tests"))
    report = {"source_parent_commit": os.environ.get("SOURCE_COMMIT", "unknown"),
              "committed_source_verified": os.environ.get("SOURCE_IS_CLEAN") == "true", "tests": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors),
              "skipped": len(result.skipped), "seconds": round(time.monotonic() - started, 3),
              "source_sha256": hashes, "scope": "isolated fixtures; no live providers or money"}
    output = Path("/evidence")
    output.mkdir(exist_ok=True)
    # Test fixtures may intentionally contain malformed UTF-16 text. Escape
    # non-ASCII in the evidence envelope so reporting cannot turn a successful
    # suite into a false non-zero exit during UTF-8 encoding.
    (output / "backend-tests.json").write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n")
    return int(not result.wasSuccessful() or bool(result.skipped))


if __name__ == "__main__":
    raise SystemExit(main())
