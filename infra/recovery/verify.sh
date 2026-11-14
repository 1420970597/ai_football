#!/bin/sh
# Isolated recovery verification only; no production configuration or image rebuild.
set -eu
storage_test_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$storage_test_root"
storage_test_id="storage-recovery-$(date +%s)-$$"
storage_test_network="ai-football-$storage_test_id"
storage_test_db="ai-football-$storage_test_id-pg"
storage_runner="ai-football-$storage_test_id-suite"
cleanup_storage_test() {
  for storage_resource in "$storage_runner" "$storage_test_db"; do
    storage_owner=$(docker inspect --format '{{index .Config.Labels "ai-football.verification-owner"}}' "$storage_resource" 2>/dev/null || true)
    if [ "$storage_owner" = "$storage_test_id" ]; then
      docker rm -f "$storage_resource" >/dev/null 2>&1 || true
    fi
  done
  storage_owner=$(docker network inspect --format '{{index .Labels "ai-football.verification-owner"}}' "$storage_test_network" 2>/dev/null || true)
  if [ "$storage_owner" = "$storage_test_id" ]; then
    docker network rm "$storage_test_network" >/dev/null 2>&1 || true
  fi
}
trap cleanup_storage_test EXIT INT TERM
docker network create --internal --label "ai-football.verification-owner=$storage_test_id" "$storage_test_network" >/dev/null
docker run -d --name "$storage_test_db" --label "ai-football.verification-owner=$storage_test_id" \
  --network "$storage_test_network" --network-alias ai-football-v2-pgtest --cpus=.4 --memory=384m \
  --tmpfs /var/lib/postgresql/data:rw,size=256m -e POSTGRES_HOST_AUTH_METHOD=trust \
  postgres:16-alpine@sha256:f4610795587492cd4fb887c2dc3de301b813278bb6f616992061141b5f15ac36 >/dev/null
storage_ready=0
until docker exec "$storage_test_db" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1; do
  storage_ready=$((storage_ready + 1))
  if [ "$storage_ready" -ge 30 ]; then exit 1; fi
  sleep 1
done
docker exec "$storage_test_db" psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -U postgres \
  -c 'CREATE ROLE recovery_test LOGIN CREATEDB' >/dev/null
docker run --rm --name "$storage_runner" --label "ai-football.verification-owner=$storage_test_id" \
  --cpus=.5 --memory=512m --network "$storage_test_network" --tmpfs /check:rw,size=64m,mode=1777 \
  -e RECOVERY_TEST_ADMIN_URL=postgresql://recovery_test@ai-football-v2-pgtest/postgres \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/check/services/api:/check/services:/check \
  -v "$storage_test_root:/source:ro" -w /check ai-football-verifier:v2.0.0-dev \
  sh -c 'cp -a /source/infra /source/services /source/tests /check/ && python -m unittest tests.test_recovery tests.test_recovery_gates tests.test_config_runtime tests.test_config_reuse tests.test_config -q'
