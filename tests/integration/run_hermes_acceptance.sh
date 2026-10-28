#!/bin/bash
# Host only orchestrates containers and frozen text copies. Python runs in Docker.
set -euo pipefail
ROOT=$(pwd)
[ -f "$ROOT/AGENTS.md" ] && [ -d "$ROOT/tests/integration" ]
TASK="af-hermes-framework-$$"
EVIDENCE="$ROOT/.runtime/$TASK"
SNAPSHOT="$EVIDENCE/snapshot"
VERIFIER="ai-football-verifier:$TASK"
HERMES_IMAGE="nousresearch/hermes-agent:v0.22.5@sha256:9c5561579f3ffdf333401797094ac3a743738d6c25d40f4331c7bf77b694238d"
PG_IMAGE="postgres:16-alpine@sha256:f4610795587492cd4fb887c2dc3de301b813278bb6f616992061141b5f15ac36"
NETWORK="$TASK-net"
PG="$TASK-pg"
API="$TASK-api"
MCP="$TASK-mcp"
PROVIDER="$TASK-provider"
HERMES="$TASK-hermes"
DSN='postgresql://postgres@ai-football-v2-pgverify:5432/v2_integration_test'
mkdir -p "$SNAPSHOT/services/collector" "$SNAPSHOT/tests/integration" "$SNAPSHOT/integrations" "$SNAPSHOT/infra/test"
cp -a services/api services/hermes-mcp "$SNAPSHOT/services/"
cp services/collector/requirements.txt "$SNAPSHOT/services/collector/"
cp infra/test/Dockerfile "$SNAPSHOT/infra/test/"
cp -a integrations/hermes "$SNAPSHOT/integrations/"
cp tests/integration/hermes_roundtrip.py tests/integration/hermes_provider_fixture.py "$SNAPSHOT/tests/integration/"
cp tests/test_hermes_fixture_contract.py "$SNAPSHOT/tests/"
sed 's/max_turns: 128/max_turns: 4/' "$SNAPSHOT/integrations/hermes/config.yaml" > "$EVIDENCE/budget-config.yaml"
cleanup() {
  local code=$?
  trap - EXIT
  for name in "$HERMES" "$MCP" "$API" "$PROVIDER" "$PG"; do
    docker logs "$name" > "$EVIDENCE/$name.log" 2>&1 || true
    docker rm -f "$name" >/dev/null 2>&1 || true
  done
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
  docker image rm "$VERIFIER" >/dev/null 2>&1 || true
  printf 'acceptance_exit=%s evidence=%s\n' "$code" "$EVIDENCE"
  exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
printf 'frozen_snapshot=%s\n' "$EVIDENCE"
find "$SNAPSHOT" -type f ! -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum > "$EVIDENCE/source.sha256"
docker build --memory 512m --cpu-period 100000 --cpu-quota 50000 \
  -f "$SNAPSHOT/infra/test/Dockerfile" -t "$VERIFIER" "$SNAPSHOT" > "$EVIDENCE/verifier-build.log" 2>&1
docker image inspect "$VERIFIER" "$HERMES_IMAGE" "$PG_IMAGE" --format '{{.Id}} {{json .RepoDigests}}' > "$EVIDENCE/images.txt"
verify() {
  docker run --rm --network "$NETWORK" --read-only --cap-drop=ALL --security-opt=no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m --pids-limit=128 -e PYTHONDONTWRITEBYTECODE=1 -v "$SNAPSHOT:/workspace:ro" -w /workspace -e PYTHONPATH=/workspace/services/api:/workspace "$@"
}
service() {
  docker run -d --network "$NETWORK" --read-only --cap-drop=ALL --security-opt=no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m --pids-limit=128 -e PYTHONDONTWRITEBYTECODE=1 -v "$SNAPSHOT:/workspace:ro" -w /workspace -e PYTHONPATH=/workspace/services/api:/workspace --label "ai-football.test=$TASK" "$@"
}
docker run --rm --network=none --cpus=.25 --memory=256m --pids-limit=64 --read-only --cap-drop=ALL --security-opt=no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m -e PYTHONDONTWRITEBYTECODE=1 -v "$SNAPSHOT:/workspace:ro" -w /workspace "$VERIFIER" python -m unittest discover -s tests -p test_hermes_fixture_contract.py -v

docker network create --internal --label "ai-football.test=$TASK" "$NETWORK" >/dev/null
docker run -d --name "$PG" --network "$NETWORK" --network-alias ai-football-v2-pgverify --cpus=.25 --memory=256m --pids-limit=128 --label "ai-football.test=$TASK" -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=v2_integration_test --tmpfs /var/lib/postgresql/data:rw,nosuid,nodev,size=256m "$PG_IMAGE" >/dev/null
for i in $(seq 1 60); do
  if docker exec "$PG" pg_isready -U postgres -d v2_integration_test >/dev/null 2>&1; then break; fi
  if [ "$i" -eq 60 ]; then exit 11; fi
  sleep .5
done
verify --cpus=.25 --memory=256m -e DATABASE_URL="$DSN" "$VERIFIER" python -c 'import asyncio,os,asyncpg; from app.database import apply_migrations; exec("async def main():\n c=await asyncpg.connect(os.environ[\"DATABASE_URL\"]); result=await apply_migrations(c); print(\"isolated_migrations\",len(result)); await c.close()\n"); asyncio.run(main())'
service --name "$API" --network-alias api --cpus=.35 --memory=384m -e DATABASE_URL="$DSN" -e APP_SESSION_SECRET=0101010101010101010101010101010101010101010101010101010101010101 -e DATA_ENCRYPTION_KEY=0202020202020202020202020202020202020202020202020202020202020202 -e EXPERT_MCP_TOKEN=isolated-tools -e COLLECTOR_INTERNAL_TOKEN=isolated-collector -e QUOTE_MAX_AGE_SECONDS=600 -e LIVE_ORDERS_ENABLED=false -e TELEGRAM_CONTROL_URL=http://absent-collector:8092 "$VERIFIER" python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 2 >/dev/null
service --name "$MCP" --network-alias hermes-mcp --cpus=.20 --memory=192m -e EXPERT_MCP_TOKEN=isolated-tools -e PLATFORM_INTERNAL_API=http://api:8080/internal/mcp "$VERIFIER" python services/hermes-mcp/server.py >/dev/null
service --name "$PROVIDER" --network-alias provider-fixture --cpus=.125 --memory=96m "$VERIFIER" python tests/integration/hermes_provider_fixture.py >/dev/null
start_hermes() {
  local config=$1
  docker run -d --name "$HERMES" --network "$NETWORK" --network-alias ai-football-v2-hermes-integration --cpus=.5 --memory=768m --pids-limit=256 --label "ai-football.test=$TASK" --read-only --cap-drop=ALL --security-opt=no-new-privileges --init --user 10000:10000 --entrypoint /opt/hermes/.venv/bin/python --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m --tmpfs /opt/data/.hermes:rw,noexec,nosuid,nodev,size=128m,mode=0700,uid=10000,gid=10000 -v "$SNAPSHOT/integrations/hermes:/workspace:ro" -v "$config:/opt/data/.hermes/config.yaml:ro" -w /workspace -e PYTHONDONTWRITEBYTECODE=1 -e HERMES_HOME=/opt/data/.hermes -e API_SERVER_ENABLED=true -e API_SERVER_HOST=0.0.0.0 -e API_SERVER_PORT=8642 -e API_SERVER_KEY=isolated-framework-key -e HERMES_MODEL=cline-pass/deepseek-v4.1-flash -e HERMES_PROVIDER_BASE_URL=http://provider-fixture:8999/v1 -e HERMES_PROVIDER_API_KEY=isolated-provider-key -e EXPERT_MCP_TOKEN=isolated-tools -e HERMES_GATEWAY_NO_SUPERVISE=1 -e ISOLATED_CANARY_SECRET=isolated-hermes-canary-never-a-real-secret "$HERMES_IMAGE" /workspace/runner.py gateway run --no-supervise >/dev/null
}
wait_services() {
  verify --cpus=.125 --memory=128m "$VERIFIER" python -c 'import httpx,time; endpoints=[("http://api:8080/readyz",{}),("http://provider-fixture:8999/evidence",{}),("http://hermes-mcp:8090/mcp",{}),("http://ai-football-v2-hermes-integration:8642/v1/models",{"Authorization":"Bearer isolated-framework-key"})]; exec("for url,headers in endpoints:\n for attempt in range(180):\n  try:\n   r=httpx.get(url,headers=headers,timeout=2,trust_env=False)\n   if r.status_code in (200,401): break\n  except httpx.HTTPError: pass\n  time.sleep(.5)\n else: raise RuntimeError(\"isolated service not ready: \"+url)\nprint(\"isolated_services_ready\")")'
}
hashes() {
  docker exec "$HERMES" python -c 'import hashlib,json; from pathlib import Path; paths=[Path("/opt/data/.hermes/config.yaml"),Path("/workspace/AGENTS.md"),*sorted(Path("/workspace/skills").rglob("SKILL.md"))]; print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},sort_keys=True))'
}
readonly_skill() {
  docker exec "$HERMES" python -c 'import errno,os; path="/workspace/skills/expert-review/.forbidden-write"; exec("try:\n fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)\nexcept OSError as exc:\n assert exc.errno in (errno.EROFS,errno.EACCES), exc.errno\n print(\"skill_mount_readonly=true\")\nelse:\n os.close(fd)\n os.unlink(path)\n raise AssertionError(\"skill mount must reject writes\")")'
}
cases() {
  verify --cpus=.25 --memory=256m -e INTEGRATION_DATABASE_URL="$DSN" -e INTEGRATION_SCENARIOS="$1" "$VERIFIER" python tests/integration/hermes_roundtrip.py
}
start_hermes "$SNAPSHOT/integrations/hermes/config.yaml"
wait_services
hashes > "$EVIDENCE/framework-before.json"
readonly_skill
cases normal,adversarial,length
printf 'restart_task_owned_framework\n'
docker restart "$HERMES" >/dev/null
wait_services
hashes > "$EVIDENCE/framework-after.json"
readonly_skill
cmp "$EVIDENCE/framework-before.json" "$EVIDENCE/framework-after.json"
printf 'framework_config_skill_hashes_unchanged=true\n'
cases normal
docker logs "$HERMES" > "$EVIDENCE/framework-main.log" 2>&1
docker rm -f "$HERMES" >/dev/null
start_hermes "$EVIDENCE/budget-config.yaml"
wait_services
cases budget
printf 'all_framework_acceptance_scenarios_passed\n'
