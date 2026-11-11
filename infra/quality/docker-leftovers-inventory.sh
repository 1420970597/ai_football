#!/bin/sh
# 只读清单：列出本项目遗留的测试容器与网络，估算可释放的内存与地址段。
# 本脚本只调用 docker ps / inspect / network ls / network inspect / stats，不含任何删除或停止命令。
#
# 纳入（名称必须匹配，且不触发下方任何保留条件）：
#   ai-football-e<数字>[字母数字]-*            历次隔离验证容器/网络
#   ai-football-sports-stats-<数字>-*          体育统计验证容器/网络
# 永不纳入：
#   ai-football-v2-* / ai-football-v2_*        在用服务、pgverify、当前任务 uxh 容器、生产网络
#   buildx_buildkit_*、bridge/host/none        构建器与内置网络
#   其余任何名称                               其他项目，或需人工判断的本项目旧容器（单列，不处理）
# 保留条件（匹配名称但本次不处理，逐条给出原因）：
#   创建时间晚于 --min-age-hours（默认 24）；是 Compose 常驻服务（非 one-off）；
#   运行中且连接了非测试网络；不在 --scope 范围内；网络仍被未纳入的容器引用。
set -eu

usage() {
  echo "用法: $0 [--min-age-hours N] [--scope all|exited|running] [--tsv] [--no-stats]"
}

docker_bin=${DOCKER_LEFTOVERS_DOCKER:-docker}
hours=24 scope=all tsv=0 stats=1
while [ $# -gt 0 ]; do
  case $1 in
    --min-age-hours) hours=${2:?}; shift 2 ;;
    --scope) scope=${2:?}; shift 2 ;;
    --tsv) tsv=1; shift ;;
    --no-stats) stats=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
case $hours in ''|*[!0-9]*) usage >&2; exit 2 ;; esac
case $scope in all|exited|running) ;; *) usage >&2; exit 2 ;; esac

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT INT TERM
now=$(date -u +%s)
cutoff_epoch=$((now - hours * 3600))
cutoff=$(date -u -d "@$cutoff_epoch" +%Y-%m-%dT%H:%M:%S)

container_format='{{.Id}}{{"\t"}}{{.Name}}{{"\t"}}{{.Created}}{{"\t"}}{{.State.Status}}{{"\t"}}{{with index .Config.Labels "com.docker.compose.project"}}{{.}}{{end}}{{"\t"}}{{with index .Config.Labels "com.docker.compose.oneoff"}}{{.}}{{end}}{{"\t"}}{{range $k,$v := .NetworkSettings.Networks}}{{$k}},{{end}}{{"\t"}}{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}},{{end}}{{end}}'
network_format='{{.Id}}{{"\t"}}{{.Name}}{{"\t"}}{{.Created}}{{"\t"}}{{if .IPAM.Config}}{{range .IPAM.Config}}{{.Subnet}} {{end}}{{else}}-{{end}}{{"\t"}}{{range .Containers}}{{.Name}},{{end}}'

# 快照必须完整：容器集合在读取期间变化（其他任务起停容器）时重试，
# 否则缺失的容器会让它占用的网络被误判为无人引用。
snapshot() {
  attempt=0
  while :; do
    "$docker_bin" ps -a --no-trunc --format '{{.ID}}' > "$work/container-ids"
    "$docker_bin" network ls --no-trunc --format '{{.ID}}' > "$work/network-ids"
    : > "$work/containers"; : > "$work/networks-raw"
    ok=1
    if [ -s "$work/container-ids" ]; then
      xargs "$docker_bin" inspect --format "$container_format" < "$work/container-ids" > "$work/containers" 2>/dev/null || ok=0
    fi
    if [ "$ok" = 1 ] && [ -s "$work/network-ids" ]; then
      xargs "$docker_bin" network inspect --format "$network_format" < "$work/network-ids" > "$work/networks-raw" 2>/dev/null || ok=0
    fi
    [ "$ok" = 1 ] && return 0
    attempt=$((attempt + 1))
    [ "$attempt" -ge 3 ] && { echo "容器或网络集合在读取期间持续变化，未生成清单；请稍后重试" >&2; exit 3; }
    sleep 2
  done
}
snapshot

# 网络创建时间带本地时区偏移，统一换算为 epoch 再比较。
: > "$work/networks"
tab=$(printf '\t')
while IFS="$tab" read -r id name created subnets attached; do
  epoch=$(date -u -d "$created" +%s 2>/dev/null) || epoch=$now
  printf '%s\t%s\t%s\t%s\t%s\n' "$id" "$name" "$epoch" "$subnets" "$attached" >> "$work/networks"
done < "$work/networks-raw"

awk -v cutoff="$cutoff" -v cutoff_epoch="$cutoff_epoch" -v hours="$hours" -v scope="$scope" '
BEGIN { FS = "\t"; OFS = "\t" }
function series(n) { return n ~ /^ai-football-e[0-9]+[a-z0-9]*-/ || n ~ /^ai-football-sports-stats-[0-9]+-/ }
function protected(n) { return n ~ /^ai-football-v2[-_]/ || n ~ /^buildx_buildkit/ }
function builtin(n) { return n == "bridge" || n == "host" || n == "none" }
function live(s) { return s == "running" || s == "restarting" || s == "paused" }
FILENAME == ARGV[1] {
  name = $2; sub(/^\//, "", name); state = $4
  count = split($7, nets, ","); reason = ""; class = "series"
  if (protected(name)) class = "protected"
  else if (!series(name)) class = (name ~ /^ai[-_]football/) ? "unmatched" : "foreign"
  else if ($5 != "" && $6 != "True") reason = "Compose常驻服务"
  else if (substr($3, 1, 19) >= cutoff) reason = "创建不足" hours "小时"
  else if (live(state)) {
    for (i = 1; i < count; i++) if (!series(nets[i]) && !builtin(nets[i])) reason = "运行中且连接非测试网络:" nets[i]
    if (reason == "" && scope == "exited") reason = "运行中(需--scope running或all)"
  } else if (scope == "running") reason = "已退出(需--scope exited或all)"
  decision = (class == "series" && reason == "") ? "remove" : "hold"
  decided[name] = decision
  if (decision == "hold") for (i = 1; i < count; i++) if (!(nets[i] in blocker)) blocker[nets[i]] = name
  volumes = ""
  count = split($8, mounted, ",")
  for (i = 1; i < count; i++) if (!(length(mounted[i]) == 64 && mounted[i] !~ /[^0-9a-f]/)) volumes = volumes mounted[i] " "
  if (class == "series") print "C", $1, name, state, substr($3, 1, 19), decision, reason, volumes
  else if (class == "unmatched") print "U", $1, name, state, substr($3, 1, 19)
  else total[class]++
  next
}
{
  name = $2; reason = ""
  if (builtin(name) || protected(name) || !series(name)) { if (!builtin(name)) total["network-" (protected(name) ? "protected" : "other")]++; next }
  if ($3 + 0 >= cutoff_epoch + 0) reason = "创建不足" hours "小时"
  else if (name in blocker) reason = "仍被未纳入容器引用:" blocker[name]
  else {
    count = split($5, attached, ",")
    for (i = 1; i < count; i++) if (decided[attached[i]] != "remove") reason = "仍被未纳入容器引用:" attached[i]
  }
  print "N", $1, name, $4, $3, (reason == "" ? "remove" : "hold"), reason
}
END { for (key in total) print "S", key, total[key] }
' "$work/containers" "$work/networks" > "$work/plan"

# 内存只对运行中的匹配容器采样；采样失败记为未知(-1)，不以 0 冒充。
: > "$work/stats"
if [ "$stats" = 1 ]; then
  awk -F'\t' '$1 == "C" && $4 == "running" { print $2 }' "$work/plan" > "$work/stat-ids"
  if [ -s "$work/stat-ids" ]; then
    xargs "$docker_bin" stats --no-stream --format '{{.Name}}{{"\t"}}{{.MemUsage}}' < "$work/stat-ids" > "$work/stats" 2>/dev/null || : > "$work/stats"
  fi
fi
awk '
BEGIN { FS = "\t"; OFS = "\t" }
function mib(v,  n) {
  n = v + 0
  if (v ~ /GiB$/) return n * 1024
  if (v ~ /MiB$/) return n
  if (v ~ /KiB$/) return n / 1024
  if (v ~ /GB$/) return n * 953.674
  if (v ~ /MB$/) return n * 0.953674
  if (v ~ /kB$/) return n / 1048.576
  return n / 1048576
}
FILENAME == ARGV[1] { split($2, usage, " "); memory[$1] = mib(usage[1]); next }
$1 == "C" { print $0, ($4 == "running" ? (($3 in memory) ? sprintf("%.1f", memory[$3]) : "-1") : "0"); next }
{ print }
' "$work/stats" "$work/plan" > "$work/tsv"

if [ "$tsv" = 1 ]; then
  cat "$work/tsv"
  exit 0
fi

awk -v hours="$hours" -v scope="$scope" -v sampled="$stats" '
BEGIN { FS = "\t" }
function pool(s) { return s ~ /^172\.(1[7-9]|2[0-9]|3[01])\./ || s ~ /^192\.168\./ }
$1 == "S" { summary[$2] = $3; next }
$1 == "U" { unmatched[++unmatched_count] = $3 " (" $4 ", " $5 ")"; next }
$1 == "C" && $6 == "remove" && $4 == "running" {
  running[++running_count] = sprintf("  %-46s %8s MiB", $3, ($9 < 0 ? "?" : $9))
  if ($9 < 0) unknown++; else freed += $9
  if ($8 != "") volumes = volumes $8
  next
}
$1 == "C" && $6 == "remove" {
  key = $3; sub(/^ai-football-e[0-9]+[a-z0-9]*-/, "ai-football-e*-", key); sub(/^ai-football-sports-stats-[0-9]+-/, "ai-football-sports-stats-*-", key)
  exited[key]++; exited_count++
  if ($8 != "") volumes = volumes $8
  next
}
$1 == "C" { held[$7]++; held_count++; if ($4 == "running") held_running[++held_running_count] = "  " $3 "  <- " $7; next }
$1 == "N" && $6 == "remove" {
  networks[++network_count] = sprintf("  %-46s %s", $3, $4)
  count = split($4, subnets, " ")
  for (i = 1; i <= count; i++) if (pool(subnets[i])) pooled++; else if (subnets[i] != "-") explicit++
  next
}
$1 == "N" { held_networks[++held_network_count] = "  " $3 "  <- " $7 }
END {
  printf "遗留测试容器与网络清单（只读；范围 --scope %s，仅含创建超过 %d 小时者）\n\n", scope, hours
  printf "可清理的运行中容器 %d 个，实测占用内存合计 %.1f MiB%s\n", running_count, freed, (unknown ? "（另有 " unknown " 个" (sampled == 1 ? "采样失败" : "未采样(--no-stats)") "，未计入）" : "")
  for (i = 1; i <= running_count; i++) print running[i]
  printf "\n可清理的已退出/未启动容器 %d 个（不占内存，占容器元数据与可写层），按名称归类：\n", exited_count
  for (key in exited) printf "  %5d  %s\n", exited[key], key | "sort -rn | head -n 15"
  close("sort -rn | head -n 15")
  printf "\n可清理的网络 %d 个：释放默认地址池段 %d 个、显式子网 %d 个\n", network_count, pooled, explicit
  for (i = 1; i <= network_count; i++) print networks[i]
  printf "\n匹配名称但本次保留的容器 %d 个：\n", held_count
  for (key in held) printf "  %5d  %s\n", held[key], key
  for (i = 1; i <= held_running_count; i++) print held_running[i]
  printf "\n匹配名称但本次保留的网络 %d 个：\n", held_network_count
  for (i = 1; i <= held_network_count; i++) print held_networks[i]
  if (volumes != "") printf "\n上述容器挂载的具名卷（清理脚本不会删除卷）：%s\n", volumes
  printf "\n未纳入、需人工判断的本项目旧容器 %d 个（名称不属于测试序列，永不自动处理）：\n", unmatched_count
  for (i = 1; i <= unmatched_count; i++) print "  " unmatched[i]
  printf "\n已排除：受保护容器 %d 个（ai-football-v2-*、buildx），其他项目容器 %d 个，受保护网络 %d 个，其他网络 %d 个\n",
    summary["protected"], summary["foreign"], summary["network-protected"], summary["network-other"]
}
' "$work/tsv"
