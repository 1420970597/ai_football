#!/usr/bin/env python3
"""Build reproducible research assets from local read-only project state."""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


REPORT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = REPORT_DIR.parents[1]
DATA_DIR = REPORT_DIR / "data"
SOURCE_CSV = REPO_ROOT / "reports" / "decision-model-accuracy" / "data" / "sources.csv"
CONFIDENCE_CSV = REPO_ROOT / "reports" / "decision-model-accuracy" / "data" / "composite_confidence_accuracy.csv"
AUDIT_JSON = REPO_ROOT / "reports" / "decision-model-accuracy" / "data" / "historical_audit.json"
DASHBOARD_JSON = REPO_ROOT / "output" / "models" / "dashboard.json"

CODE_PATHS = (
    "core/live_model.py",
    "core/ensemble.py",
    "core/decision_features.py",
    "core/learned_model.py",
    "service/model_training.py",
    "service/model_service.py",
    "store/training_data.py",
)

NEW_SOURCE = {
    "reference": "S25",
    "source_id": "market-calibrated-inplay-2026",
    "title": "Market-calibrated in-play football forecasting",
    "grade": "A-",
    "verified_content": "arXiv abstract: market-calibrated team strength plus post-shot xG; 140 EPL matches",
    "retrieval_url": "https://arxiv.org/abs/2605.16066",
    "sha256": "6fdded6257e1c7d06750c0987126b30a55e03ef1d6bb4c71ff326a45728eb980",
    "limitation": "Small English Premier League sample and Betfair setting; direction only, no transfer claim",
}

PRIORITIES = [
    {
        "priority": "P0",
        "work": "重建可追溯标签与双时间 as-of 数据集",
        "impact": "极高",
        "evidence": "A: 904,473 条异常同伴概率、79,972 条标签翻转",
        "cost": "中",
        "leakage_risk": "降低最高",
        "owner": "store/training_data.py + history repair",
        "acceptance": "每行有 match/market/line/outcome/cutoff/received_at/label_available_at；未来值审计为0",
    },
    {
        "priority": "P0",
        "work": "拆分 HAD top-1、亚洲盘支付状态和概率评分",
        "impact": "极高",
        "evidence": "A: 当前 metrics 把三向 HAD 当三个二分类",
        "cost": "低",
        "leakage_risk": "降低",
        "owner": "core/learned_model.py + model worker",
        "acceptance": "固定测试集同时输出 top-1、W/L/U、Brier、log loss、coverage",
    },
    {
        "priority": "P1",
        "work": "市场概率 offset Logistic/GAM 残差校准",
        "impact": "高",
        "evidence": "A: GLM offset；S25 市场校准方向性证据",
        "cost": "低-中",
        "leakage_risk": "中",
        "owner": "core/learned_model.py",
        "acceptance": "滚动 OOF 配对 Brier/log loss 优于市场或标记不确定；delta=0 可退回市场",
    },
    {
        "priority": "P1",
        "work": "固定时间窗口的 fair-logit 走势与报价质量特征",
        "impact": "中-高",
        "evidence": "A: 当前最近40 tick 不编码时间间距",
        "cost": "中",
        "leakage_risk": "高，需双时间审计",
        "owner": "service/live_expert.py + decision_features.py",
        "acceptance": "离线/在线同截点特征哈希一致；窗口消融在主要盘口不退化",
    },
    {
        "priority": "P1",
        "work": "同一盘口算法输出改为 OOF 残差融合并做概率一致性",
        "impact": "中-高",
        "evidence": "A: 当前多算法共享盘口，线性平均存在相关重复计权风险",
        "cost": "中",
        "leakage_risk": "中-高",
        "owner": "core/ensemble.py + validation",
        "acceptance": "OOF 元模型；HAD/OU/AH 概率和支付状态约束通过",
    },
    {
        "priority": "P2",
        "work": "受限 LightGBM/XGBoost 挑战模型",
        "impact": "未知",
        "evidence": "A: 官方参数文档；表格基准不保证足球增益",
        "cost": "中-高",
        "leakage_risk": "高",
        "owner": "独立 model worker",
        "acceptance": "严格时间+比赛分组 OOF；CPU/RSS/p99 预算内且显著优于 offset",
    },
    {
        "priority": "P2",
        "work": "动态/双变量 Poisson 或 hazard 作为结构化挑战",
        "impact": "中",
        "evidence": "A: 经典足球计数过程文献；当前数据完整性仍是前置条件",
        "cost": "高",
        "leakage_risk": "中",
        "owner": "core/live_model.py 的独立实现",
        "acceptance": "目标盘口留出、低比分/红牌子群不退化；推理 p99 < 2 秒链路预算",
    },
    {
        "priority": "P3",
        "work": "影子前瞻、漂移监控、晋级/回滚",
        "impact": "高（风险控制）",
        "evidence": "A: 现有 worker 已有 shadow/active 分离",
        "cost": "中",
        "leakage_risk": "降低",
        "owner": "service/model_service.py + dashboard",
        "acceptance": "候选/市场/当前模型同一输入；按比赛成组置信区间；自动回退不丢版本",
    },
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    if not path.exists():
        return {"missing": str(path)}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_snapshot() -> None:
    manifest = []
    for relative in CODE_PATHS:
        path = REPO_ROOT / relative
        manifest.append({
            "path": relative,
            "exists": path.exists(),
            "sha256": sha256(path) if path.exists() else "",
            "lines": len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0,
        })
    audit = read_json(AUDIT_JSON)
    dashboard = read_json(DASHBOARD_JSON)
    confidence_rows = []
    if CONFIDENCE_CSV.exists():
        with CONFIDENCE_CSV.open(newline="", encoding="utf-8") as handle:
            confidence_rows = list(csv.DictReader(handle))
    non_empty = [row for row in confidence_rows if row.get("scope") == "all" and int(row.get("records", 0) or 0) > 0]
    write_json(DATA_DIR / "current_snapshot.json", {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository": str(REPO_ROOT),
        "code_manifest": manifest,
        "dashboard": dashboard,
        "historical_audit": audit,
        "confidence_non_empty_bands": non_empty,
        "limitations": [
            "dashboard and SQLite sources are read independently; repair workers may change counts",
            "historical audit retains contaminated features and is diagnostic, not a clean retrain",
            "confidence bands contain pending decisions and are not a production betting claim",
        ],
    })


def build_sources() -> None:
    fields = ["reference", "source_id", "title", "grade", "verified_content", "retrieval_url", "accessed_at", "sha256", "limitation"]
    rows = []
    if SOURCE_CSV.exists():
        with SOURCE_CSV.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                row.setdefault("limitation", "见报告证据范围与适用边界")
                rows.append(row)
    rows.append({**NEW_SOURCE, "accessed_at": datetime.now(timezone.utc).isoformat()})
    with (DATA_DIR / "research_sources.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in fields} for row in rows)


def build_priorities() -> None:
    fields = ["priority", "work", "impact", "evidence", "cost", "leakage_risk", "owner", "acceptance"]
    with (DATA_DIR / "priority_matrix.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(PRIORITIES)


def build_confidence() -> None:
    if not CONFIDENCE_CSV.exists():
        raise FileNotFoundError(CONFIDENCE_CSV)
    with CONFIDENCE_CSV.open(newline="", encoding="utf-8") as source:
        rows = [row for row in csv.DictReader(source) if row.get("scope") == "all"]
    if not rows:
        raise ValueError("综合置信度 CSV 没有 all scope")
    with (DATA_DIR / "confidence_accuracy.csv").open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    build_snapshot()
    build_sources()
    build_priorities()
    build_confidence()
    print("built", DATA_DIR)


if __name__ == "__main__":
    main()
