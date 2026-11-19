#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../.."
name="af-hermes-connection-$$"
frozen="$PWD/.runtime/$name"
mkdir -p "$frozen"
cp -a services integrations tests web "$frozen/"
pg=postgres:16-alpine@sha256:f4610795587492cd4fb887c2dc3de301b813278bb6f616992061141b5f15ac36
hermes=nousresearch/hermes-agent:v0.22.5@sha256:9c5561579f3ffdf333401797094ac3a743738d6c25d40f4331c7bf77b694238d
verifier=ai-football-verifier:v2.0.0-dev
browser=ai-football-browser:v2.0.0-dev
token=$(printf '12%.0s' {1..32})
storage_key=$(printf '34%.0s' {1..32})
dsn=postgresql://postgres@postgres:5432/v2_hermes_connection
cleanup() {
  for service in web hermes provider api pg; do
    docker logs "$name-$service" > "$frozen/$service.log" 2>&1 || true
  done
  docker rm -f "$name-web" "$name-hermes" "$name-provider" "$name-api" "$name-pg" >/dev/null 2>&1 || true
  docker network rm "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker network create --internal "$name" >/dev/null
docker run -d --name "$name-pg" --network "$name" --network-alias postgres --cpus=.25 --memory=256m \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=v2_hermes_connection --tmpfs /var/lib/postgresql/data \
  "$pg" >/dev/null
for i in {1..30}; do
  if docker exec "$name-pg" pg_isready -h 127.0.0.1 -U postgres >/dev/null; then break; fi
  if [[ "$i" == 30 ]]; then exit 1; fi
  sleep 1
done
run=(docker run --network "$name" -v "$frozen:/workspace:ro" -e PYTHONDONTWRITEBYTECODE=1)
"${run[@]}" --rm --cpus=.25 --memory=192m -e DATABASE_URL="$dsn" "$verifier" python -c \
 'import asyncio,os,asyncpg; from app.database import apply_migrations; exec("async def main():\n c=await asyncpg.connect(os.environ[\"DATABASE_URL\"]); await apply_migrations(c); await c.close()"); asyncio.run(main())'
"${run[@]}" -d --name "$name-api" --network-alias api --cpus=.35 --memory=384m \
  -e DATABASE_URL="$dsn" -e COOKIE_SECURE=false -e BOOTSTRAP_ADMIN_EMAIL=fixture-admin \
  -e BOOTSTRAP_ADMIN_PASSWORD=isolated-connection-admin-pass -e HERMES_CONNECTION_TOKEN="$token" \
  -e HERMES_API_KEY=isolated-hermes-gateway-token-xyz-890 -e COLLECTOR_INTERNAL_TOKEN=fixture-collector \
  -e EXPERT_MCP_TOKEN=fixture-mcp -e APP_SESSION_SECRET="$(printf '56%.0s' {1..32})" \
  -e DATA_ENCRYPTION_KEY="$(printf '78%.0s' {1..32})" \
  "$verifier" python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 >/dev/null
"${run[@]}" -d --name "$name-provider" --network-alias provider-fixture --cpus=.15 --memory=96m \
  "$verifier" python tests/integration/hermes_connection_provider.py >/dev/null
wait_http() {
  "${run[@]}" --rm --cpus=.15 --memory=96m "$verifier" python -c \
    'import sys,time,httpx; exec("for _ in range(60):\n try:\n  if httpx.get(sys.argv[1],timeout=1).status_code==200: break\n except httpx.HTTPError: pass\n time.sleep(.5)\nelse: raise RuntimeError(sys.argv[1])")' "$1"
}
wait_http http://api:8080/readyz
docker run -d --name "$name-hermes" --network "$name" --network-alias hermes --network-alias hermes-connection \
  --init --cpus=.55 --memory=900m --user 10000:10000 --entrypoint /opt/hermes/.venv/bin/python \
  --tmpfs /opt/data/.hermes:rw,uid=10000,gid=10000,mode=0700,size=128m \
  -v "$frozen/integrations/hermes:/workspace:ro" \
  -v "$frozen/integrations/hermes/config.yaml:/opt/data/.hermes/config.yaml:ro" -w /workspace \
  -e PYTHONDONTWRITEBYTECODE=1 -e HERMES_HOME=/opt/data/.hermes -e API_SERVER_ENABLED=true \
  -e API_SERVER_HOST=0.0.0.0 -e API_SERVER_PORT=8642 -e API_SERVER_KEY=isolated-hermes-gateway-token-xyz-890 \
  -e HERMES_MODEL=fixture-baseline -e HERMES_PROVIDER_BASE_URL=http://provider-fixture:8999/v1 \
  -e HERMES_PROVIDER_API_KEY=fixture-key-one -e HERMES_CONNECTION_TOKEN="$token" \
  -e HERMES_CONNECTION_STORAGE_KEY="$storage_key" -e HERMES_GATEWAY_NO_SUPERVISE=1 -e HERMES_STREAM_RETRIES=0 \
  -e EXPERT_MCP_TOKEN=fixture-mcp "$hermes" /workspace/runner.py gateway run --no-supervise >/dev/null
wait_http http://hermes:8642/health
docker run -d --name "$name-web" --network "$name" --network-alias web --cpus=.2 --memory=128m \
  -v "$frozen/web/app/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  -v "$frozen/web/app/index.html:/usr/share/nginx/html/index.html:ro" \
  -v "$frozen/web/app:/usr/share/nginx/html/assets:ro" \
  nginx:1.29-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de >/dev/null
checks() {
  docker run --rm --network "$name" --cpus=.5 --memory=512m --shm-size=128m --pids-limit=256 \
    -e VERIFY_RESTART="$1" -v "$frozen/tests/browser:/work:ro" "$browser" hermes-connection.mjs
}
wait_http http://web/healthz
checks 0
docker exec "$name-pg" psql -v ON_ERROR_STOP=1 -U postgres -d v2_hermes_connection -tAc \
  "SELECT (SELECT count(*) FROM v2_audit_log WHERE action='hermes_connection_updated')=5
    AND (SELECT encrypted_value NOT LIKE '%fixture-key%' FROM v2_admin_settings WHERE setting_key='_hermes_connection_v1')" |
  rg -qx 't'
docker restart "$name-hermes" >/dev/null
wait_http http://hermes:8642/health
checks 1
