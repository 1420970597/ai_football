#!/bin/sh
# 清理本项目遗留的测试容器与网络。默认 dry-run：只打印清单，不删除任何东西。
# 真正删除必须显式传 --execute。选择规则全部来自同目录的只读清单脚本
# docker-leftovers-inventory.sh（名称模式、创建时间、保留条件见该文件头部）。
#
# 执行时的额外防线：
#   - 按容器/网络 ID 操作；删除前逐个重新 inspect，名称或运行状态与清单不一致即跳过；
#   - 本脚本自身再次校验名称，任何 ai-football-v2* 或不属于测试序列的名称一律拒绝；
#   - 不删除镜像和具名卷；匿名卷仅在 --anonymous-volumes 时随所属容器删除；
#   - 网络用 docker network rm，仍有容器连接时 Docker 自身会拒绝。
set -eu

usage() {
  echo "用法: $0 [--min-age-hours N] [--scope all|exited|running] [--anonymous-volumes] [--execute]"
  echo "  不带 --execute 时只打印将要清理的清单（dry-run）。"
}

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
docker_bin=${DOCKER_LEFTOVERS_DOCKER:-docker}
hours=24 scope=all execute=0 volumes=0
while [ $# -gt 0 ]; do
  case $1 in
    --min-age-hours) hours=${2:?}; shift 2 ;;
    --scope) scope=${2:?}; shift 2 ;;
    --anonymous-volumes) volumes=1; shift ;;
    --execute) execute=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

if [ "$execute" != 1 ]; then
  sh "$here/docker-leftovers-inventory.sh" --min-age-hours "$hours" --scope "$scope"
  echo
  echo "DRY-RUN：未删除、未停止任何容器或网络。确认上述清单后加 --execute 才会执行。"
  exit 0
fi

allowed() {
  case $1 in
    ai-football-v2*|buildx_buildkit*) return 1 ;;
  esac
  printf '%s\n' "$1" | grep -Eq '^ai-football-(e[0-9]+[a-z0-9]*|sports-stats-[0-9]+)-[A-Za-z0-9_.-]+$'
}

plan=$(mktemp)
trap 'rm -f "$plan"' EXIT INT TERM
sh "$here/docker-leftovers-inventory.sh" --min-age-hours "$hours" --scope "$scope" --tsv --no-stats > "$plan"

tab=$(printf '\t')
removed=0 skipped=0 failed=0
while IFS="$tab" read -r kind id name state created decision rest; do
  [ "$kind" = C ] && [ "$decision" = remove ] || continue
  if ! allowed "$name"; then
    echo "拒绝（名称不在测试序列内）: $name"; skipped=$((skipped + 1)); continue
  fi
  current=$("$docker_bin" inspect --format '{{.Name}} {{.State.Status}}' "$id" 2>/dev/null) || {
    echo "跳过（已不存在）: $name"; skipped=$((skipped + 1)); continue; }
  if [ "$current" != "/$name $state" ]; then
    echo "跳过（清单生成后状态已变化）: $name"; skipped=$((skipped + 1)); continue
  fi
  if [ "$volumes" = 1 ]; then set -- rm -f -v "$id"; else set -- rm -f "$id"; fi
  if "$docker_bin" "$@" >/dev/null; then
    echo "已删除容器: $name"; removed=$((removed + 1))
  else
    echo "删除失败: $name" >&2; failed=$((failed + 1))
  fi
done < "$plan"

networks=0
while IFS="$tab" read -r kind id name subnets created decision rest; do
  [ "$kind" = N ] && [ "$decision" = remove ] || continue
  if ! allowed "$name"; then
    echo "拒绝（名称不在测试序列内）: $name"; skipped=$((skipped + 1)); continue
  fi
  current=$("$docker_bin" network inspect --format '{{.Name}}' "$id" 2>/dev/null) || {
    echo "跳过（已不存在）: $name"; skipped=$((skipped + 1)); continue; }
  if [ "$current" != "$name" ]; then
    echo "跳过（清单生成后已变化）: $name"; skipped=$((skipped + 1)); continue
  fi
  if "$docker_bin" network rm "$id" >/dev/null; then
    echo "已删除网络: $name ($subnets)"; networks=$((networks + 1))
  else
    echo "删除失败（可能仍有容器连接）: $name" >&2; failed=$((failed + 1))
  fi
done < "$plan"

echo "完成：删除容器 $removed 个、网络 $networks 个，跳过 $skipped 个，失败 $failed 个"
[ "$failed" -eq 0 ]
