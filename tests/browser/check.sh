#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
task_network="ai-football-web-verify-$$"
task_evidence="$task_root/.runtime/browser-ui-$$"
mkdir -p "$task_evidence"
cleanup() {
  task_result=$?
  docker network rm "$task_network" >/dev/null 2>&1 || true
  exit "$task_result"
}
trap cleanup EXIT INT TERM
docker build --memory 768m --cpu-period 100000 --cpu-quota 50000 -f "$task_root/tests/browser/Dockerfile" -t ai-football-browser:v2.0.0-dev "$task_root"
# Explicit subnet: Docker's default address pools may be exhausted on this host.
docker network create --internal --subnet "${BROWSER_TEST_SUBNET:-10.93.78.16/28}" \
  --label ai-football.task=web-browser "$task_network" >/dev/null
docker run --rm --network "$task_network" --cpus .75 --memory 1g --pids-limit 256 \
  -e BROWSER_EVIDENCE_DIR=/evidence -e BROWSER_RESULTS=/evidence/results.json \
  -v "$task_root:/work:ro" -v "$task_evidence:/evidence" \
  ai-football-browser:v2.0.0-dev tests/browser/verify.mjs
docker run --rm --network "$task_network" --cpus .5 --memory 768m --pids-limit 128 \
  -e BROWSER_EVIDENCE_DIR=/evidence \
  -v "$task_root:/work:ro" -v "$task_evidence:/evidence" \
  ai-football-browser:v2.0.0-dev tests/browser/sports-expert-statistics.mjs
docker run --rm --network "$task_network" --cpus .5 --memory 768m --pids-limit 128 \
  -e BROWSER_EVIDENCE_DIR=/evidence \
  -v "$task_root:/work:ro" -v "$task_evidence:/evidence" \
  ai-football-browser:v2.0.0-dev tests/browser/basketball-score-display.mjs
docker run --rm --network "$task_network" --cpus .5 --memory 768m --pids-limit 128 \
  -e BROWSER_EVIDENCE_DIR=/evidence \
  -v "$task_root:/work:ro" -v "$task_evidence:/evidence" \
  ai-football-browser:v2.0.0-dev tests/browser/homepage-expert-matches.mjs
docker run --rm --network "$task_network" --cpus .5 --memory 768m --pids-limit 128 \
  -e BROWSER_EVIDENCE_DIR=/evidence \
  -v "$task_root:/work:ro" -v "$task_evidence:/evidence" \
  ai-football-browser:v2.0.0-dev tests/browser/homepage-account-history.mjs
docker run --rm --network "$task_network" --cpus .5 --memory 768m --pids-limit 128 \
  -e BROWSER_EVIDENCE_DIR=/evidence \
  -v "$task_root:/work:ro" -v "$task_evidence:/evidence" \
  ai-football-browser:v2.0.0-dev tests/browser/first-screen-requests.mjs
docker run --rm --network "$task_network" --cpus .5 --memory 768m --pids-limit 128 \
  -e BROWSER_EVIDENCE_DIR=/evidence \
  -v "$task_root:/work:ro" -v "$task_evidence:/evidence" \
  ai-football-browser:v2.0.0-dev tests/browser/accessibility.mjs
printf 'Browser evidence: %s\n' "$task_evidence/results.json"
