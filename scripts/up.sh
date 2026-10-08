#!/usr/bin/env bash
# 启动 ai_football 全栈（B/S 架构）
#
# 核心原则：**绝不触碰宿主机上其他项目的服务。**
#   - 若某端口被*其他项目*占用 → 仅为本项目挑选一个空闲端口
#   - 若该端口已被*本项目*容器占用 → 复用之（保证脚本幂等，避免端口漂移）
#   - 不停止、不修改、不重启任何非本项目容器
#
# 用法：
#   ./scripts/up.sh              # 幂等启动（自动避让他人端口）
#   ./scripts/up.sh --dry-run    # 只打印将使用的端口，不启动
#   ./scripts/up.sh --down       # 仅停止本项目
#
# 环境变量覆盖：REDIS_PORT ANALYTICS_PORT CONSOLE_PORT

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT}/docker/docker-compose.yml"
PROJECT_NAME="ai_football"

MODE="up"
case "${1:-}" in
  --dry-run) MODE="dry" ;;
  --down)    MODE="down" ;;
  "")        ;;
  *) echo "未知参数：$1" >&2; exit 2 ;;
esac

if [ "$MODE" = "down" ]; then
  echo "==> 仅停止本项目（${PROJECT_NAME}），不影响其他服务"
  docker compose -f "$COMPOSE_FILE" down
  exit 0
fi

# ---------------------------------------------------------------------------
# 端口探测
# ---------------------------------------------------------------------------

# 返回 0 表示该端口已被 *本项目* 的容器占用（视为可复用）
port_held_by_own_project() {
  local port="${1:?需要 port}"
  local names
  names="$(docker ps -a --filter "label=com.docker.compose.project=${PROJECT_NAME}" \
             --format '{{.Ports}}' 2>/dev/null || true)"
  printf '%s\n' "$names" | grep -qE "[:.]${port}->"
}

# 返回 0 表示端口被 *其他东西* 占用（监听中、或他人容器已发布）
port_held_by_others() {
  local port="${1:?需要 port}"

  # 他人容器的端口映射
  local other_ports
  other_ports="$(docker ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null \
                   | grep -v "^${PROJECT_NAME}" || true)"
  if printf '%s\n' "$other_ports" | grep -qE "[:.]${port}->"; then
    return 0
  fi

  # 宿主机监听中的端口
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$" && return 0
  fi
  return 1
}

pick_port() {
  local preferred="${1:?pick_port 需要 preferred}"
  local name="${2:?pick_port 需要 name}"
  local limit=$((preferred + 200))
  local p="$preferred"

  while [ "$p" -le "$limit" ]; do
    if port_held_by_own_project "$p"; then
      echo "  ${name}: ${p}（本项目已占用，复用）" >&2
      echo "$p"; return 0
    fi
    if ! port_held_by_others "$p"; then
      if [ "$p" != "$preferred" ]; then
        echo "  ${name}: ${preferred} 被其他项目占用 → 改用 ${p}" >&2
      else
        echo "  ${name}: ${p}（空闲）" >&2
      fi
      echo "$p"; return 0
    fi
    p=$((p + 1))
  done
  echo "  ❌ ${name}: 在 ${preferred}–${limit} 范围内找不到可用端口" >&2
  return 1
}

echo "==> 探测端口（仅为本项目选端口，不影响其他服务）"
REDIS_PORT="${REDIS_PORT:-$(pick_port 6379 REDIS_PORT)}"
ANALYTICS_PORT="${ANALYTICS_PORT:-$(pick_port 8000 ANALYTICS_PORT)}"
CONSOLE_PORT="${CONSOLE_PORT:-$(pick_port 3000 CONSOLE_PORT)}"

export REDIS_PORT ANALYTICS_PORT CONSOLE_PORT

cat <<EOF

==> 将使用以下端口
    redis           127.0.0.1:${REDIS_PORT}
    analytics-api   0.0.0.0:${ANALYTICS_PORT}
    web-console     0.0.0.0:${CONSOLE_PORT}

    控制台入口: http://localhost:${CONSOLE_PORT}/
    健康检查:   http://localhost:${ANALYTICS_PORT}/health
EOF

if [ "$MODE" = "dry" ]; then
  echo
  echo "==> --dry-run：未启动任何容器"
  exit 0
fi

echo
echo "==> 构建并启动"
docker compose -f "$COMPOSE_FILE" up -d --build

# nginx 配置是 **bind mount**（./nginx.console.conf），且容器镜像未变时
# `up -d` 不会重启 web-console —— 于是改了配置也不会生效，
# 表现为“改了超时但仍然 504”。因此这里显式 reload。
docker compose -f "$COMPOSE_FILE" exec -T web-console nginx -s reload \
  >/dev/null 2>&1 && echo "    nginx 配置已重载" || true

echo
echo "==> 等待 analytics-api 就绪"
READY=0
for i in $(seq 1 30); do
  if curl -fsS --max-time 3 "http://localhost:${ANALYTICS_PORT}/health" >/dev/null 2>&1; then
    echo "    已就绪（第 ${i} 次探测）"
    READY=1
    break
  fi
  sleep 2
done
[ "$READY" = "1" ] || echo "    ⚠️ 30 次探测未就绪，请查看容器日志"

echo
docker compose -f "$COMPOSE_FILE" ps

cat <<EOF

==> 完成
    控制台:  http://localhost:${CONSOLE_PORT}/
    健康:    curl -s http://localhost:${ANALYTICS_PORT}/health

    停止本项目（不影响其他服务）:
      ./scripts/up.sh --down
EOF
