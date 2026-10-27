#!/usr/bin/env bash
set -euo pipefail
TASK_PERF_ROOT=$(git rev-parse --show-toplevel)
cd "$TASK_PERF_ROOT"
TASK_PERF_IMAGE=${PERFORMANCE_IMAGE:-ai-football-verifier:v2.0.0-dev}
TASK_PERF_NETWORK=ai-football-v2-performance
TASK_PERF_PG=ai-football-v2-perf-pg
TASK_PERF_API=ai-football-v2-perf-api
TASK_PERF_CREATED_NETWORK=false
TASK_PERF_CREATED_PG=false
TASK_PERF_CREATED_API=false
cleanup() {
  if "$TASK_PERF_CREATED_API"; then docker rm -f "$TASK_PERF_API" >/dev/null; fi
  if "$TASK_PERF_CREATED_PG"; then docker rm -fv "$TASK_PERF_PG" >/dev/null; fi
  if "$TASK_PERF_CREATED_NETWORK"; then docker network rm "$TASK_PERF_NETWORK" >/dev/null; fi
}
trap cleanup EXIT
for TASK_PERF_NAME in "$TASK_PERF_PG" "$TASK_PERF_API"; do
  if docker container inspect "$TASK_PERF_NAME" >/dev/null 2>&1; then
    printf '%s\n' 'This task container already exists; refusing to replace it.' >&2
    exit 1
  fi
done
if docker network inspect "$TASK_PERF_NETWORK" >/dev/null 2>&1; then
  printf '%s\n' 'This task network already exists; refusing to replace it.' >&2
  exit 1
fi
mkdir -p reports/performance
docker network create --internal "$TASK_PERF_NETWORK" >/dev/null
TASK_PERF_CREATED_NETWORK=true
docker run -d --name "$TASK_PERF_PG" --network "$TASK_PERF_NETWORK" --cpus=0.35 --memory=384m \
  --tmpfs /var/lib/postgresql/data:rw,nosuid,size=268435456 \
  -e POSTGRES_USER=performance_test -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=postgres \
  postgres:16-alpine -c shared_buffers=32MB -c max_connections=32 -c max_parallel_workers_per_gather=0 >/dev/null
TASK_PERF_CREATED_PG=true
for TASK_PERF_WAIT in $(seq 1 30); do
  if docker exec "$TASK_PERF_PG" pg_isready -U performance_test -d postgres >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$TASK_PERF_PG" pg_isready -U performance_test -d postgres >/dev/null
docker run --rm --network "$TASK_PERF_NETWORK" --cpus=0.15 --memory=256m \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/w/services/api:/w \
  -e PERFORMANCE_SYNTHETIC_ONLY=true -v "$TASK_PERF_ROOT:/w:ro" -w /w \
  "$TASK_PERF_IMAGE" python tests/performance/seed.py > reports/performance/seed.json
docker run -d --name "$TASK_PERF_API" --network "$TASK_PERF_NETWORK" --cpus=0.25 --memory=256m \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/w/services/api:/w \
  -e DATABASE_URL=postgresql://performance_test@ai-football-v2-perf-pg/test_performance_baseline \
  -e APP_SESSION_SECRET=0000000000000000000000000000000000000000000000000000000000000000 \
  -e DATA_ENCRYPTION_KEY=1111111111111111111111111111111111111111111111111111111111111111 \
  -e EXPERT_MCP_TOKEN=synthetic-performance-tools -e COLLECTOR_INTERNAL_TOKEN=synthetic-performance-collector \
  -e QUOTE_MAX_AGE_SECONDS=3600 -e LIVE_ORDERS_ENABLED=false \
  -e HERMES_API_URL=http://127.0.0.1:9/v1 -v "$TASK_PERF_ROOT:/w:ro" -w /w \
  "$TASK_PERF_IMAGE" uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1 --no-access-log >/dev/null
TASK_PERF_CREATED_API=true
docker run --rm --user 0 --network "$TASK_PERF_NETWORK" --cpus=0.15 --memory=256m \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/w/services/api:/w \
  -e PERFORMANCE_SYNTHETIC_ONLY=true -e PERFORMANCE_SOURCE_COMMIT="$(git rev-parse HEAD)" \
  -e PERFORMANCE_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$TASK_PERF_IMAGE")" \
  -e PERFORMANCE_PG_IMAGE_ID="$(docker image inspect --format '{{.Id}}' postgres:16-alpine)" \
  -v "$TASK_PERF_ROOT:/w:ro" -v "$TASK_PERF_ROOT/reports/performance:/w/reports/performance:rw" -w /w \
  "$TASK_PERF_IMAGE" python tests/performance/benchmark.py
docker stats --no-stream --format '{{json .}}' "$TASK_PERF_PG" "$TASK_PERF_API" > reports/performance/resources.jsonl
docker exec "$TASK_PERF_PG" cat /sys/fs/cgroup/cpu.stat > reports/performance/postgres-cpu.stat
docker exec "$TASK_PERF_API" cat /sys/fs/cgroup/cpu.stat > reports/performance/api-cpu.stat
docker logs "$TASK_PERF_API" > reports/performance/api.log 2>&1
printf '%s\n' 'Synthetic performance reports saved; task API, PG and internal network are removed on exit.'
