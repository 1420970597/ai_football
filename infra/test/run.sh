#!/bin/sh
# Host work is limited to Docker, Git and project-local evidence directories.
set -eu
test_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$test_root"
test_suffix="$$"
test_network="ai-football-v2-ci-$test_suffix"
test_database="ai-football-v2-ci-pg-$test_suffix"
test_runner="ai-football-v2-ci-suite-$test_suffix"
test_output="$test_root/.runtime/test-evidence/$test_suffix"
cleanup() {
  docker rm -f "$test_runner" "$test_database" >/dev/null 2>&1 || true
  docker network rm "$test_network" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM
mkdir -p "$test_output"
docker build -f infra/test/Dockerfile -t ai-football-verifier:v2.0.0-dev .
docker network create --internal "$test_network" >/dev/null
docker run -d --name "$test_database" --network "$test_network" \
  --network-alias ai-football-v2-pgverify --network-alias ai-football-v2-pgtest \
  --cpus=.5 --memory=384m --tmpfs /var/lib/postgresql/data:rw,size=256m \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  postgres:16-alpine@sha256:f4610795587492cd4fb887c2dc3de301b813278bb6f616992061141b5f15ac36 >/dev/null
test_ready=0
while ! docker exec "$test_database" pg_isready -U postgres >/dev/null 2>&1; do
  test_ready=$((test_ready + 1))
  if [ "$test_ready" -ge 30 ]; then exit 1; fi
  sleep 1
done
docker exec -i "$test_database" psql -v ON_ERROR_STOP=1 -U postgres >/dev/null <<'SQL'
CREATE ROLE renewal_protocol_test LOGIN PASSWORD 'isolated-renewal-test-only';
CREATE DATABASE renewal_protocol_test OWNER renewal_protocol_test;
CREATE ROLE account_state_test LOGIN CREATEDB;
SQL
test_commit=$(git rev-parse HEAD)
docker run --rm --name "$test_runner" --user "$(id -u):$(id -g)" \
  --cpus=.75 --memory=512m --network "$test_network" \
  -e PYTHONDONTWRITEBYTECODE=1 -e SOURCE_COMMIT="$test_commit" \
  -e TEST_DATABASE_ADMIN_URL=postgresql://postgres@ai-football-v2-pgverify:5432/postgres \
  -e IDENTITY_TEST_DATABASE_URL=postgresql://postgres@ai-football-v2-pgverify:5432/postgres \
  -e TELEGRAM_TEST_DATABASE_URL=postgresql://postgres@ai-football-v2-pgverify:5432/postgres \
  -e LEYU_RENEWAL_TEST_DSN=postgresql://renewal_protocol_test:isolated-renewal-test-only@ai-football-v2-pgtest/renewal_protocol_test \
  -e ACCOUNT_TEST_DATABASE_URL=postgresql://account_state_test@ai-football-v2-pgtest/postgres \
  -v "$test_root:/source:ro" -v "$test_output:/evidence" \
  ai-football-verifier:v2.0.0-dev python /source/infra/test/frozen_suite.py
printf 'Isolated test evidence: %s\n' "$test_output"
