#!/bin/sh
# Host work is limited to Docker, Git and project-local evidence directories.
#
# Usage: infra/test/run.sh [unittest target ...]
#   No target runs the whole suite; targets are dotted names such as
#   tests.test_reviews or tests.test_reviews.ReviewTests.
# Environment:
#   TEST_SUBNET      subnet of the internal test network (default 10.93.78.0/28);
#                    Docker's default address pools may be exhausted on this host
#   TEST_REBUILD=1   rebuild the verifier image even if its requirements match
#                    (needed after editing infra/test/Dockerfile itself)
#   TEST_LOCK_FILE   serialize with other test runs on this host through flock
#   TEST_LOCK_WAIT   seconds to wait for that lock (default 5400)
# The last line printed is "exit=N" and N is also this script's exit status, so
# the result survives a caller that pipes the output without pipefail.
set -eu
test_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$test_root"
if [ -n "${TEST_LOCK_FILE:-}" ]; then
  exec 9>"$TEST_LOCK_FILE"
  flock -w "${TEST_LOCK_WAIT:-5400}" 9 || { echo 'test lock timeout' >&2; exit 3; }
fi
test_suffix="$$"
test_image=ai-football-verifier:v2.0.0-dev
test_network="ai-football-v2-ci-$test_suffix"
test_database="ai-football-v2-ci-pg-$test_suffix"
test_runner="ai-football-v2-ci-suite-$test_suffix"
test_output="$test_root/.runtime/test-evidence/$test_suffix"
cleanup() {
  docker rm -f "$test_runner" "$test_database" >/dev/null 2>&1 || true
  docker network rm "$test_network" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$test_output"
# The image holds only the interpreter and the three requirement sets it was
# built from; sources are mounted. Rebuild only when those sets differ.
image_is_current() {
  [ "${TEST_REBUILD:-0}" != 1 ] || return 1
  docker image inspect "$test_image" >/dev/null 2>&1 || return 1
  test_installed=$(docker run --rm --network none --entrypoint cat "$test_image" \
    /tmp/api-requirements.txt /tmp/collector-requirements.txt /tmp/mcp-requirements.txt | sha256sum)
  [ "$test_installed" = "$(cat services/api/requirements.txt services/collector/requirements.txt \
    services/hermes-mcp/requirements.txt | sha256sum)" ]
}
if image_is_current; then
  printf 'Reusing %s (requirements unchanged)\n' "$test_image"
else
  docker build -f infra/test/Dockerfile -t "$test_image" .
fi
docker network create --internal --subnet "${TEST_SUBNET:-10.93.78.0/28}" "$test_network" >/dev/null
# Keep the original isolated budget: .5 CPU PostgreSQL plus .75 CPU runner.
docker run -d --name "$test_database" --network "$test_network" \
  --network-alias ai-football-v2-pgverify --network-alias ai-football-v2-pgtest \
  --cpus=.5 --memory=384m --tmpfs /var/lib/postgresql/data:rw,size=256m \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  postgres:16-alpine@sha256:f4610795587492cd4fb887c2dc3de301b813278bb6f616992061141b5f15ac36 >/dev/null
test_ready=0
while ! docker exec "$test_database" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1; do
  test_ready=$((test_ready + 1))
  if [ "$test_ready" -ge 30 ]; then exit 1; fi
  sleep 1
done
docker exec -i "$test_database" psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -U postgres >/dev/null <<'SQL'
CREATE ROLE renewal_protocol_test LOGIN PASSWORD 'isolated-renewal-test-only';
CREATE DATABASE renewal_protocol_test OWNER renewal_protocol_test;
CREATE ROLE account_state_test LOGIN CREATEDB;
CREATE ROLE recovery_test LOGIN CREATEDB;
SQL
test_commit=$(git rev-parse HEAD)
test_source_clean=false
if [ -z "$(git status --porcelain --untracked-files=normal)" ]; then test_source_clean=true; fi
test_status=0
docker run --rm --name "$test_runner" --user "$(id -u):$(id -g)" \
  --cpus=.75 --memory=512m --network "$test_network" \
  -e PYTHONDONTWRITEBYTECODE=1 -e SOURCE_COMMIT="$test_commit" -e SOURCE_IS_CLEAN="$test_source_clean" \
  -e TEST_DATABASE_ADMIN_URL=postgresql://postgres@ai-football-v2-pgverify:5432/postgres \
  -e IDENTITY_TEST_DATABASE_URL=postgresql://postgres@ai-football-v2-pgverify:5432/postgres \
  -e TELEGRAM_TEST_DATABASE_URL=postgresql://postgres@ai-football-v2-pgverify:5432/postgres \
  -e LEYU_RENEWAL_TEST_DSN=postgresql://renewal_protocol_test:isolated-renewal-test-only@ai-football-v2-pgtest/renewal_protocol_test \
  -e ACCOUNT_TEST_DATABASE_URL=postgresql://account_state_test@ai-football-v2-pgtest/postgres \
  -e RECOVERY_TEST_ADMIN_URL=postgresql://recovery_test@ai-football-v2-pgtest/postgres \
  -v "$test_root:/source:ro" -v "$test_output:/evidence" \
  "$test_image" python /source/infra/test/frozen_suite.py "$@" || test_status=$?
printf '%s\n' "$test_status" > "$test_output/exit-code"
printf 'Isolated test evidence: %s\n' "$test_output"
printf 'exit=%s\n' "$test_status"
exit "$test_status"
