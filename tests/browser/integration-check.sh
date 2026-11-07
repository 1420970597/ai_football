#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
task_project="web-integration-$$"
task_evidence="$task_root/.runtime/$task_project"
task_snapshot="$task_evidence/snapshot"
mkdir -p "$task_snapshot/tests" "$task_snapshot/services" "$task_snapshot/web" "$task_snapshot/reports/jiebao-score-reference/images"
cp -a "$task_root/services/api" "$task_snapshot/services/"
cp -a "$task_root/web/app" "$task_snapshot/web/"
cp -a "$task_root/tests/browser" "$task_root/tests/integration" "$task_snapshot/tests/"
export BROWSER_API_IMAGE="ai-football-api:$task_project"
export BROWSER_WEB_IMAGE="ai-football-web:$task_project"
task_compose="$task_snapshot/tests/browser/compose.integration.yml"
task_override="$task_snapshot/tests/integration/compose.browser-accounts.yml"
task_extended=false
compose() {
 if [ "$task_extended" = true ]; then
  docker compose -p "$task_project-extended" -f "$task_compose" -f "$task_override" "$@"
 else
  docker compose -p "$task_project" -f "$task_compose" "$@"
 fi
}
cleanup() {
 task_result=$?
 trap - EXIT INT TERM
 if [ "$task_extended" = true ]; then
  compose logs --no-color api seed web account-fixture > "$task_evidence/service-final.log" 2>&1 || true
 else
  compose logs --no-color api seed web > "$task_evidence/service-final.log" 2>&1 || true
 fi
 compose down --volumes --remove-orphans >/dev/null 2>&1 || true
 printf 'browser_acceptance_exit=%s evidence=%s\n' "$task_result" "$task_evidence"
 exit "$task_result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
printf 'browser_frozen_snapshot=%s\n' "$task_evidence"
find "$task_snapshot/services" "$task_snapshot/web" "$task_snapshot/tests/integration" -type f ! -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum > "$task_evidence/source.sha256"
compose build --memory 768m seed web
compose up -d --wait --wait-timeout 90 web
compose run --rm --no-deps browser
cp "$task_snapshot/tests/browser/integration-results.json" "$task_evidence/integration-results.json"
compose logs --no-color api seed > "$task_evidence/service-baseline.log" 2>&1
compose down --volumes --remove-orphans >/dev/null
task_extended=true
compose up -d --wait --wait-timeout 90 web account-fixture
compose run --rm --no-deps browser
cp "$task_snapshot/tests/browser/integration-account-results.json" "$task_evidence/integration-account-results.json"
