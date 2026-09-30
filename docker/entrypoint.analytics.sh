#!/bin/sh
# analytics-api 容器启动脚本
#
# 职责：
#   1) 若宿主机语料存在且快照库为空 → 先导入（保证控制台开箱即有数据）
#   2) 启动 REST API + 静态控制台
#
# 设计说明：
#   - 导入是**幂等**的：归一化层按 (match_id, market, captured_at, odds)
#     去重，重复启动不会产生重复快照。
#   - 导入失败不阻塞启动：API 仍需可用于诊断。
set -e

: "${SNAPSHOT_ROOT:=/app/output/snapshots}"
: "${CORPUS_ROOT:=/app/output}"
: "${API_HOST:=0.0.0.0}"
: "${API_PORT:=8000}"

export SNAPSHOT_ROOT CORPUS_ROOT

echo "[entrypoint] SNAPSHOT_ROOT=${SNAPSHOT_ROOT}"
echo "[entrypoint] CORPUS_ROOT=${CORPUS_ROOT}"

# ---- 1) 语料导入（仅当快照库为空 & 语料存在） ----
SNAP_COUNT=$(find "${SNAPSHOT_ROOT}" -name '*.json' ! -name '_index.json' 2>/dev/null | wc -l | tr -d ' ')
CORPUS_COUNT=$(find "${CORPUS_ROOT}" -maxdepth 1 -name '场次*.json' 2>/dev/null | wc -l | tr -d ' ')

echo "[entrypoint] 现有快照=${SNAP_COUNT}  语料场次=${CORPUS_COUNT}"

if [ "${SNAP_COUNT}" = "0" ] && [ "${CORPUS_COUNT}" != "0" ]; then
  echo "[entrypoint] 快照库为空，开始导入语料 ..."
  python3 - <<'PY' || echo "[entrypoint] 导入失败（不阻塞启动）"
import json
import os
import sys

sys.path.insert(0, "/app")

from service.valuation import ValuationService

root = os.environ.get("SNAPSHOT_ROOT", "/app/output/snapshots")
corpus = os.environ.get("CORPUS_ROOT", "/app/output")
svc = ValuationService(snapshot_root=root, corpus_root=corpus)
res = svc.ingest_corpus()
print("[entrypoint] 导入结果: %s" % json.dumps(
    {k: res[k] for k in ("records", "snapshots", "written", "issues")},
    ensure_ascii=False))
PY
else
  echo "[entrypoint] 跳过导入（已有快照或无语料）"
fi

# ---- 2) 启动 API ----
echo "[entrypoint] 启动 API http://${API_HOST}:${API_PORT}"
exec python3 -m api.app
