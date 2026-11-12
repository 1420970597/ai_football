"""Seal only successful isolated browser results and the exact public artifacts."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import struct

ROOT = Path(__file__).resolve().parents[2]
# 期望条数必须写在封存器里：它证明跑的是哪一版场景集合，而不只是"没报错"。
# 数值随脚本增删场景而变，改这里时必须同时说明是哪次提交增删了场景。
CHECKS = (
    # 25 → 68：2550e106（2026-10-26）之后持续扩充场景，并因 d1961418
    # 删除 3 项换机/再次验证场景。以当前 verify.mjs 实跑结果为准（68 项全 PASS、无重复名）。
    ("tests/browser/results.json", "sh tests/browser/check.sh", 68),
    # 11 → 17：4ec64643（2026-10-27）补齐账户浏览器成功链，新增 6 个场景
    # （注册登录、管理员来源/套餐/会员授予、本人绑定、限额与停用、撤销会员）。
    ("tests/browser/integration-results.json", "sh tests/browser/integration-check.sh", 17),
    ("tests/browser/report-results.json", "Docker network none: tests/browser/report-check.mjs", 3),
    # 29 → 23：d1961418（2026-10-30）按 AGENTS.md 七.4 移除二次验证门禁，
    # 删掉 8 项换机/MFA 检查、新增 2 项「系统设置不提供二次验证入口」。
    ("tests/browser/accessibility-results.json", "sh tests/browser/accessibility-check.sh", 23),
)


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def snapshot():
    checks = []
    for relative, command, expected in CHECKS:
        data = json.loads((ROOT / relative).read_text())
        rows = data.get("results", data.get("checks"))
        assert len(rows) == expected
        assert not data["browser_errors"]
        if "external_requests" in data:
            assert not data["external_requests"]
            assert all(row["diagram_loaded"] and row["source_readable"] for row in data["results"])
        else:
            assert all(row["status"] == "passed" for row in rows)
        if "scans" in data:
            # 38 → 30：同上 d1961418，减去 10 项 MFA/换机/取消弹层扫描、
            # 加上 2 项 admin-system 扫描。
            assert len(data["scans"]) == 30
            assert not any(row["violations"] for row in data["scans"])
            assert all(row["state"] == "css_colors_measured" and
                       (row["decorative"] or row["minimum_ratio"] >= row["threshold"])
                       for scan in data["scans"] for row in scan["color_review"])
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
        "browser_test_source_sha256": {str(path.relative_to(ROOT)): digest(path)
                                       for path in sorted((ROOT / "tests/browser").iterdir())
                                       if path.is_file() and (path.suffix in {".mjs", ".sh", ".py", ".yml"}
                                                              or path.name in {"Dockerfile", "package.json", "package-lock.json"})},
        "report_html_sha256": digest(ROOT / "reports/jiebao-score-reference/REPORT.html"),
        "screenshots_and_diagrams": images,
        "upstream_limits": ["No real Leyu or Telegram login", "No real money orders", "S3 intentionally unconfigured; expected private media 503", "No provider vision verification", "No production freshness or configuration ack"],
    }
    target = ROOT / "docs/prototypes/WEB_VERIFICATION_SNAPSHOT.json"
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"checks": [row["passed"] for row in checks], "images": len(images)}))


if __name__ == "__main__":
    snapshot()
