"""Seal only successful isolated browser results and the exact public artifacts."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import struct

ROOT = Path(__file__).resolve().parents[2]
CHECKS = (
    ("tests/browser/results.json", "sh tests/browser/check.sh", 24),
    ("tests/browser/integration-results.json", "sh tests/browser/integration-check.sh", 11),
    ("tests/browser/report-results.json", "Docker network none: tests/browser/report-check.mjs", 3),
)


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def snapshot():
    checks = []
    for relative, command, expected in CHECKS:
        data = json.loads((ROOT / relative).read_text())
        assert len(data["results"]) == expected
        assert not data["browser_errors"]
        if "external_requests" in data:
            assert not data["external_requests"]
            assert all(row["diagram_loaded"] and row["source_readable"] for row in data["results"])
        else:
            assert all(row["status"] == "passed" for row in data["results"])
        checks.append({"command": command, "exit_code": 0, "result": relative, "passed": expected,
                       "sha256": digest(ROOT / relative), "limits": data["environment"]})
    images = []
    for path in sorted((ROOT / "reports/jiebao-score-reference/images").iterdir()):
        if path.suffix not in {".png", ".svg"}:
            continue
        item = {"path": str(path.relative_to(ROOT)), "sha256": digest(path),
                "artificial_sample": "fixture" in path.name}
        if path.suffix == ".png":
            raw = path.read_bytes()
            assert raw[:8] == b"\x89PNG\r\n\x1a\n"
            item["width"], item["height"] = struct.unpack(">II", raw[16:24])
        images.append(item)
    commit = os.environ.get("VERIFICATION_SOURCE_COMMIT", "")
    assert not commit or re.fullmatch(r"[0-9a-f]{40}", commit)
    data = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "stage": "isolated_validation_passed; not production or release",
        "web_source_commit": commit or None,
        "source_note": "Frozen candidate web files are identified by exact hashes. A commit is supplied only after this source has been committed; null means a not-yet-committed candidate. Report/docs are a separate verification artifact.",
        "checks": checks,
        "web_source_sha256": {str(path.relative_to(ROOT)): digest(path) for path in sorted((ROOT / "web/app").iterdir()) if path.is_file()},
        "report_html_sha256": digest(ROOT / "reports/jiebao-score-reference/REPORT.html"),
        "screenshots_and_diagrams": images,
        "upstream_limits": ["No real Leyu or Telegram login", "No real money orders", "S3 intentionally unconfigured; expected private media 503", "No provider vision verification", "No production freshness or configuration ack"],
    }
    target = ROOT / "docs/prototypes/WEB_VERIFICATION_SNAPSHOT.json"
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"checks": [row["passed"] for row in checks], "images": len(images)}))


if __name__ == "__main__":
    snapshot()
