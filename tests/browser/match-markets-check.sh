#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
task_evidence="$task_root/.runtime/match-markets-ui-$$"
task_snapshot="$task_evidence/source"
task_network="ai-football-match-markets-$$"
cleanup() {
  task_result=$?
  docker network rm "$task_network" >/dev/null 2>&1 || true
  exit "$task_result"
}
trap cleanup EXIT INT TERM
mkdir -p "$task_snapshot/web" "$task_snapshot/tests/browser"
cp -a "$task_root/web/app" "$task_snapshot/web/"
cp "$task_root/tests/browser/match-markets.mjs" "$task_snapshot/tests/browser/"
docker image inspect ai-football-browser:v2.0.0-dev >/dev/null
docker run --rm --cpus=.25 --memory=128m --network none --entrypoint sh ai-football-browser:v2.0.0-dev -ec 'command -v npx >/dev/null'
docker network create --internal --label ai-football.task=match-market-pagination "$task_network" >/dev/null
docker run --rm --cpus=.5 --memory=512m --shm-size=128m --pids-limit=256 --network "$task_network" \
  -e MATCH_MARKETS_RESULTS=/evidence -v "$task_snapshot:/work:ro" -v "$task_evidence:/evidence" -w /work \
  ai-football-browser:v2.0.0-dev tests/browser/match-markets.mjs
printf 'Match market pagination evidence: %s\n' "$task_evidence/results.json"
