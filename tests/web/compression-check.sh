#!/bin/sh
# 隔离验证 web/app/nginx.conf 的压缩边界：静态脚本/样式与公开 JSON 压缩，
# 含登录态的 /api/ 与 /healthz 不压缩。使用内部网络与替身上游，不连接任何真实服务。
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
task_image=$(sed -n 's/^FROM //p' "$task_root/web/app/Dockerfile" | head -n 1)
task_network="ai-football-web-gzip-$$"
task_api="ai-football-web-gzip-api-$$"
task_web="ai-football-web-gzip-web-$$"
mkdir -p "$task_root/.runtime"
task_tmp=$(mktemp -d "$task_root/.runtime/web-gzip-XXXXXX")
cleanup() {
  if [ "${WEB_GZIP_DEBUG:-0}" = 1 ]; then docker logs "$task_web" 2>&1 | tail -n 8 >&2 || true; docker logs "$task_api" 2>&1 | tail -n 5 >&2 || true; fi
  task_result=$?
  docker rm -f "$task_web" "$task_api" >/dev/null 2>&1 || true
  docker network rm "$task_network" >/dev/null 2>&1 || true
  rm -rf "$task_tmp"
  exit "$task_result"
}
trap cleanup EXIT INT TERM
# 替身上游：固定返回约 6 KB 的 JSON 文件，足以越过 gzip_min_length。
mkdir -p "$task_tmp/data"
awk 'BEGIN{printf "{\"items\":["; for(i=0;i<200;i++) printf "%s{\"id\":%d,\"name\":\"synthetic\"}", (i?",":""), i; printf "]}"}' > "$task_tmp/data/body.json"
cat > "$task_tmp/api.conf" <<'CONF'
server {
    listen 8080;
    root /data;
    default_type application/json;
    location / { try_files /body.json =404; }
}
CONF
chmod -R a+rX "$task_tmp"
docker network create --internal --subnet "${WEB_GZIP_SUBNET:-10.93.77.16/28}" \
  --label ai-football.task=web-gzip "$task_network" >/dev/null
docker run -d --name "$task_api" --network "$task_network" --network-alias api \
  --cpus .25 --memory 64m -v "$task_tmp/api.conf:/etc/nginx/conf.d/default.conf:ro" -v "$task_tmp/data:/data:ro" "$task_image" >/dev/null
docker run -d --name "$task_web" --network "$task_network" --cpus .25 --memory 64m \
  -v "$task_root/web/app/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  -v "$task_root/web/app/index.html:/usr/share/nginx/html/index.html:ro" \
  -v "$task_root/web/app/app.js:/usr/share/nginx/html/assets/app.js:ro" \
  -v "$task_root/web/app/app.css:/usr/share/nginx/html/assets/app.css:ro" \
  "$task_image" >/dev/null
task_ready=0
until docker exec "$task_web" curl -fsS -o /dev/null http://127.0.0.1/healthz 2>/dev/null; do
  task_ready=$((task_ready + 1))
  if [ "$task_ready" -ge 30 ]; then echo "web stub did not become ready" >&2; exit 1; fi
  sleep 1
done
probe() {
  docker exec "$task_web" curl -sS -o /dev/null -H "Accept-Encoding: $2" \
    -w '%{http_code} %{size_download} %header{content-encoding} %header{vary}' "http://127.0.0.1$1"
}
task_failed=0
expect() { # path accept-encoding expected-encoding
  task_line=$(probe "$1" "$2")
  task_status=$(printf '%s' "$task_line" | cut -d' ' -f1)
  task_encoding=$(printf '%s' "$task_line" | cut -d' ' -f3)
  printf '%-34s accept=%-8s -> %s\n' "$1" "$2" "$task_line"
  if [ "$task_status" != 200 ] || [ "$task_encoding" != "$3" ]; then
    printf 'FAIL %s: expected status 200 encoding "%s"\n' "$1" "$3" >&2
    task_failed=1
  fi
}
expect /assets/app.js gzip gzip
expect /assets/app.css gzip gzip
expect /api/v1/public/matches gzip gzip
expect /assets/app.js identity ""
expect /api/v1/public/matches identity ""
expect /api/v1/auth/me gzip ""
expect /api/v1/accounts/leyu gzip ""
expect /api/v1/admin/me gzip ""
expect /healthz gzip ""
task_plain=$(probe /assets/app.js identity | cut -d' ' -f2)
task_packed=$(probe /assets/app.js gzip | cut -d' ' -f2)
printf 'app.js bytes plain=%s gzip=%s\n' "$task_plain" "$task_packed"
if [ "$task_packed" -ge "$task_plain" ]; then echo 'FAIL compressed script is not smaller' >&2; task_failed=1; fi
for task_blocked in /internal/x /mcp/x /hermes/x; do
  task_code=$(docker exec "$task_web" curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1$task_blocked")
  printf '%-34s -> %s\n' "$task_blocked" "$task_code"
  if [ "$task_code" != 404 ]; then echo "FAIL $task_blocked must stay 404" >&2; task_failed=1; fi
done
exit "$task_failed"
