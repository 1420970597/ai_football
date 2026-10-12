#!/usr/bin/env bash
# CPU 受限地跑「编译 / 构建 / 测试」这类会打满多核的命令。
#
# 为什么需要它：本机同时跑着 LLM 网关（pi 的对话链路指向它）。一旦某个
# 构建把 4 个核全占满，网关请求就会被饿死，表现为**对话中断/超时**。
# 所以要把这类批处理命令钉在固定的 2 个核（4 vCPU 的 50%）上。
#
# 用法：
#   scripts/cpu-limited.sh build                 # 受限构建本项目镜像
#   scripts/cpu-limited.sh run -- python3 -m unittest discover -s tests -q
#   scripts/cpu-limited.sh run -- python3 -m mypy
#
# 环境变量：
#   AI_FOOTBALL_CPU_PERCENT  允许占用的宿主机 CPU 百分比（默认 50）
#   AI_FOOTBALL_CPUSET       可选的硬钉核集（如 "0-1"）；设了就用 cpuset 而非 quota
#   AI_FOOTBALL_CPUS         容器运行期限额（默认 0.0，不限制；独立于构建预算）
#
# ---------------------------------------------------------------------------
# 实测结论（勿"简化"掉这段，我踩过）：
#
#   * BuildKit **没有**任何 CPU 限制开关：`docker build` 丢掉了 legacy 的
#     --cpu-quota/--cpuset-cpus（`docker build --help` 里搜不到）。构建容器
#     实测 cpu.max = `max 100000`，即无上限。
#
#   * `taskset` 包裹 docker build **无效** —— 构建容器由 dockerd/runc 派生，
#     不继承客户端的亲和性掩码。实测 `taskset -c 0-1 docker build` 的构建
#     容器仍看到 affinity=[0,1,2,3]。即 docker CLI 只是一个 API 客户端：
#     进程级限制加在客户端上，限制不到服务端拉起的构建容器。
#
#   * `--cgroup-parent=<slice>` 对 BuildKit 也无效，实测切片始终 Tasks: 0。
#
#   * 真正有效的两条路（均需 DOCKER_BUILDKIT=0 退到 legacy builder）：
#       --cpu-quota=<n*period>  → 实测 cpu.max = `200000 100000`（= 2 核配额）
#       --cpuset-cpus=0-1       → 实测 affinity=[0,1]、cpuset.effective=0-1
#     默认用 quota：它是「配额」语义，与核编号无关，换机器改百分比即可；
#     cpuset 会把任务硬钉到具体核上（有时与宿主其他负载争夺同一批核）。
#
#   * 非 docker 的普通命令（unittest/mypy/ruff）直接用 taskset —— 它们真
#     是脚本的子进程，亲和性可继承。
# ---------------------------------------------------------------------------

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT}/docker/docker-compose.yml"

# 默认占宿主 CPU 的 50%。配额是硬上限（不是权重）：空闲时也拦得住。
CPU_PERCENT="${AI_FOOTBALL_CPU_PERCENT:-50}"
CPUSET="${AI_FOOTBALL_CPUSET:-}"

NPROC="$(nproc)"
# CFS 周期固定 100ms（=100000us，docker 默认）。配额 = 百分比 × 核数 × 周期。
CFS_PERIOD=100000
CPU_QUOTA=$(( NPROC * CFS_PERIOD * CPU_PERCENT / 100 ))
# 构建预算以 cores 表示；运行期配额单独配置。
CORES="$(python3 -c "print(f'{$CPU_PERCENT / 100 * $NPROC:.1f}')")"
# Build/test limits are independent of the runtime quota; 0.0 is unlimited.
export AI_FOOTBALL_CPUS="${AI_FOOTBALL_CPUS:-0.0}"

# 非 docker 命令（unittest/mypy/ruff）用 taskset 钉核。核数**向下取整**，
# 保证「最多」不超过百分比（50% × 4 核 → 2 核 = cpu 0-1）。
# 钉核与配额的差别：配额是「总量上限」，进程仍可短暂跑在所有核上；
# 钉核是「物理隔离」，把核让给同机的 LLM 网关。本地重命令用后者更稳。
RUN_CORES=$(( NPROC * CPU_PERCENT / 100 ))
if [ "$RUN_CORES" -lt 1 ]; then RUN_CORES=1; fi
RUN_CPUSET="${CPUSET:-0-$(( RUN_CORES - 1 ))}"

usage() {
  cat >&2 <<EOF
用法：
  $(basename "$0") build [服务名...]        # 受限构建（默认 all）
  $(basename "$0") run -- <命令> [参数...]  # 受限执行任意命令
  $(basename "$0") up                       # 启动容器（运行期已带 cpus）

当前 CPU 预算：宿主 ${NPROC} 核 × ${CPU_PERCENT}% = ${CORES} 核
              构建 quota=${CPU_QUOTA}/$CFS_PERIOD（legacy builder）
              容器 cpus=${AI_FOOTBALL_CPUS}
              本地命令 taskset -c ${RUN_CPUSET}
覆盖：AI_FOOTBALL_CPU_PERCENT=30 或 AI_FOOTBALL_CPUSET=0-1
EOF
  exit 2
}

# 关到 legacy builder（BuildKit 不支持 CPU 限制）。
legacy() { env DOCKER_BUILDKIT=0 "$@"; }

# 解析 compose 中所有「需要构建」的服务，输出 TSV：name<TAB>image<TAB>dockerfile<TAB>context
# 注意：compose 里 analytics-api **没有写 image:**，镜像名由 compose 按
# `{project}-{service}` 推导（ai_football-analytics-api）。早期版本要求
# image 非空才构建，于是一个服务都没构建却报「跳过」——别再用那个判据。
compose_build_services() {
  docker compose -f "$COMPOSE_FILE" config --format json | python3 -c "
import json, sys
proj = json.load(sys.stdin)
pname = proj['name']
for name, s in proj['services'].items():
    b = s.get('build') or {}
    if not b.get('dockerfile'):
        continue
    img = s.get('image') or f'{pname}-{name}'
    print('\t'.join([name, img, b['dockerfile'], b.get('context') or '.']))
"
}

cmd="${1:-}"
[ -n "$cmd" ] || usage
shift || true

case "$cmd" in
  # ---- 构建：必须退到 legacy builder，否则 CPU 限制被忽略（见文件头）----
  build)
    [ "$#" -gt 0 ] || set -- ""   # 空参数 = 全部可构建服务
    want=("$@")
    built=0
    while IFS=$'\t' read -r svc image dockerfile ctx; do
      [ -n "$svc" ] || continue
      # 指定了服务名时过滤（空参数表示全部）
      if [ -n "${want[0]}" ]; then
        hit=0
        for w in "${want[@]}"; do [ "$w" = "$svc" ] && hit=1; done
        [ "$hit" = 1 ] || continue
      fi
      # compose 的 context 是相对 compose 文件目录解析的。
      case "$ctx" in
        /*) abs_ctx="$ctx" ;;
        *)  abs_ctx="$(cd "$(dirname "$COMPOSE_FILE")/$ctx" && pwd)" ;;
      esac
      abs_df="$abs_ctx/$dockerfile"
      echo "==> 受限构建 $svc（quota=${CPU_QUOTA} = ${CPU_PERCENT}% × ${NPROC} 核）"
      echo "    image    ：$image"
      echo "    Dockerfile：$abs_df"
      echo "    ⚠️ legacy builder 与 BuildKit 缓存不互通，首次会全量重建。"
      if [ -n "$CPUSET" ]; then
        legacy docker build --cpu-quota="$CPU_QUOTA" --cpuset-cpus="$CPUSET" \
          -t "$image" -f "$abs_df" "$abs_ctx"
      else
        legacy docker build --cpu-quota="$CPU_QUOTA" \
          -t "$image" -f "$abs_df" "$abs_ctx"
      fi
      built=$(( built + 1 ))
    done < <(compose_build_services)
    [ "$built" -gt 0 ] || { echo "没有匹配到需要构建的服务：${want[*]}" >&2; exit 1; }
    ;;

  # ---- 任意命令：taskset 直接限定亲和性 -------------------------------
  run)
    if [ "${1:-}" = "--" ]; then
      shift
    fi
    [ "$#" -gt 0 ] || usage
    set -- taskset -c "$RUN_CPUSET" "$@"
    echo "==> 受限执行（taskset -c ${RUN_CPUSET}，≈${CPU_PERCENT}% × ${NPROC} 核）：$*"
    exec "$@"
    ;;

  # ---- 直接起容器：交给 compose，它已带 cpus --------------------------
  up)
    echo "==> 启动（容器运行期 cpus=${AI_FOOTBALL_CPUS}）"
    exec env AI_FOOTBALL_CPUS="${AI_FOOTBALL_CPUS}" \
      docker compose -f "$COMPOSE_FILE" up -d "$@"
    ;;

  *) usage ;;
esac
