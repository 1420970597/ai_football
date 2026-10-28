#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
task_network="ai-football-a11y-$$"
task_evidence="$task_root/.runtime/accessibility-ui-$$"
mkdir -p "$task_evidence"
cleanup() {
  task_result=$?
  docker network rm "$task_network" >/dev/null 2>&1 || true
  exit "$task_result"
}
trap cleanup EXIT INT TERM
docker build --memory 768m --cpu-period 100000 --cpu-quota 50000 -f "$task_root/tests/browser/Dockerfile" -t ai-football-browser:v2.0.0-dev "$task_root"
docker network create --internal --label ai-football.task=web-accessibility "$task_network" >/dev/null
docker run --rm --network "$task_network" --cpus .75 --memory 1g --pids-limit 256 \
  -e BROWSER_EVIDENCE_DIR=/evidence -e BROWSER_ACCESSIBILITY_RESULTS=/evidence/results.json \
  -v "$task_root:/work:ro" -v "$task_evidence:/evidence" \
  ai-football-browser:v2.0.0-dev tests/browser/verify.mjs --a11y "$@"
printf 'Accessibility evidence: %s\n' "$task_evidence/results.json"
