#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
task_evidence="$task_root/.runtime/visual-ui-$$"
task_snapshot="$task_evidence/source"
task_network="ai-football-visual-$$"
cleanup() {
  task_result=$?
  docker network rm "$task_network" >/dev/null 2>&1 || true
  exit "$task_result"
}
trap cleanup EXIT INT TERM
mkdir -p "$task_snapshot/web" "$task_snapshot/tests/browser"
cp -a "$task_root/web/app" "$task_snapshot/web/"
cp "$task_root/tests/browser/visual-fixture.mjs" "$task_root/tests/browser/visual-pixels.mjs" \
  "$task_root/tests/browser/visual-pixels-test.mjs" "$task_root/tests/browser/visual-regression.mjs" "$task_snapshot/tests/browser/"
cp -a "$task_root/tests/browser/visual-baselines" "$task_snapshot/tests/browser/"
docker image inspect ai-football-browser:v2.0.0-dev >/dev/null
docker network create --internal --label ai-football.task=visual-regression "$task_network" >/dev/null
docker run --rm --cpus=.5 --memory=256m --network none -v "$task_snapshot:/work:ro" -w /work \
  ai-football-browser:v2.0.0-dev tests/browser/visual-pixels-test.mjs
docker run --rm --cpus=.5 --memory=512m --shm-size=128m --pids-limit=256 --network "$task_network" \
  -e VISUAL_RESULTS=/evidence -v "$task_snapshot:/work:ro" -v "$task_evidence:/evidence" -w /work \
  ai-football-browser:v2.0.0-dev tests/browser/visual-regression.mjs
printf 'Visual evidence: %s\n' "$task_evidence/visual-results.json"
