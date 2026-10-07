#!/bin/sh
# analytics-api 容器启动脚本
#
# 职责：启动 REST API + 静态控制台。
#
# 数据源只有**乐鱼（leyu）**。快照由采集链路实时写入 SNAPSHOT_ROOT；
# 2026-10 起已下线「体彩语料导入」步骤（体彩源与 corpus 概念一并移除）。
set -e

: "${SNAPSHOT_ROOT:=/app/output/snapshots}"
: "${API_HOST:=0.0.0.0}"
: "${API_PORT:=8000}"

export SNAPSHOT_ROOT

echo "[entrypoint] SNAPSHOT_ROOT=${SNAPSHOT_ROOT}"

echo "[entrypoint] 启动 API http://${API_HOST}:${API_PORT}"
exec python3 -m api.app
