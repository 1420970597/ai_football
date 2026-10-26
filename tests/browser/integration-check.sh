#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
task_project="web-integration-$$"
compose() { docker compose -p "$task_project" -f "$task_root/tests/browser/compose.integration.yml" "$@"; }
cleanup() {
 task_result=$?
 compose logs --no-color api seed > "$task_root/tests/browser/integration-service.log" 2>&1 || true
 compose down --volumes --remove-orphans >/dev/null 2>&1 || true
 exit "$task_result"
}
trap cleanup EXIT INT TERM
compose build --memory 768m seed web
compose up -d --wait --wait-timeout 90 web
compose run --rm --no-deps browser
